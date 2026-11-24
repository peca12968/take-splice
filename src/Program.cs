using System.Globalization;
using System.Text;
using System.Text.Json;

internal sealed record Clip(string Source, long Start, long End, long OutputStart);
internal sealed record Take(FileStream Stream, Wave Wave);

internal static class Program
{
    private static int Main(string[] args)
    {
        uint fadeFrames = 0;
        if (args.Length != 2 && (args.Length != 4 || args[2] != "--edge-fade-frames" ||
            !uint.TryParse(args[3], NumberStyles.None, CultureInfo.InvariantCulture, out fadeFrames)))
        {
            Console.Error.WriteLine("usage: take-splice PLAN.tsv NEW-OUTPUT.wav [--edge-fade-frames N]");
            return 2;
        }
        try
        {
            Render(Path.GetFullPath(args[0]), Path.GetFullPath(args[1]), fadeFrames);
            return 0;
        }
        catch (Exception ex) when (ex is IOException or InvalidDataException or UnauthorizedAccessException
                                   or ArgumentException or OverflowException or NotSupportedException)
        {
            Console.Error.WriteLine(ex.Message);
            return 2;
        }
    }

    private static void Render(string plan, string target, uint fadeFrames)
    {
        if (File.Exists(target)) throw new IOException("output already exists");
        string directory = Path.GetDirectoryName(plan)!;
        var takes = new Dictionary<string, Take>(StringComparer.Ordinal);
        var clips = new List<Clip>();
        Pcm? format = null;
        long total = 0;
        try
        {
            using var lines = File.ReadLines(plan, new UTF8Encoding(false, true)).GetEnumerator();
            if (!lines.MoveNext() || lines.Current != "source\tstart_frame\tend_frame")
                throw new InvalidDataException("expected source, start_frame and end_frame TSV header");
            while (lines.MoveNext())
            {
                string[] fields = lines.Current.Split('\t');
                int line = clips.Count + 2;
                if (fields.Length != 3 || string.IsNullOrWhiteSpace(fields[0]) ||
                    !long.TryParse(fields[1], NumberStyles.None, CultureInfo.InvariantCulture, out long start) ||
                    !long.TryParse(fields[2], NumberStyles.None, CultureInfo.InvariantCulture, out long end) || end <= start)
                    throw new InvalidDataException($"line {line}: expected file and nonempty half-open frame interval");
                string path = Path.GetFullPath(fields[0], directory);
                if (!takes.TryGetValue(path, out var take))
                {
                    var stream = File.OpenRead(path);
                    try { take = new Take(stream, Wave.Read(stream)); }
                    catch { stream.Dispose(); throw; }
                    takes.Add(path, take);
                }
                format ??= take.Wave.Format;
                if (take.Wave.Format != format || end > take.Wave.Frames)
                    throw new InvalidDataException($"line {line}: format mismatch or range exceeds source length");
                clips.Add(new Clip(fields[0], start, end, total));
                total = checked(total + (end - start));
            }
            if (format == null) throw new InvalidDataException("empty edit list");
            string staging = Path.Combine(Path.GetDirectoryName(target)!, $".take-{Guid.NewGuid():N}.tmp");
            var output = new FileStream(staging, FileMode.CreateNew, FileAccess.Write, FileShare.None);
            try
            {
                using (output)
                {
                    Wave.WriteHeader(output, format, total);
                    byte[] buffer = new byte[65536];
                    int framesPerBlock = buffer.Length / format.FrameBytes;
                    for (int index = 0; index < clips.Count; index++)
                    {
                        var clip = clips[index];
                        var take = takes[Path.GetFullPath(clip.Source, directory)];
                        take.Stream.Position = take.Wave.DataOffset + clip.Start * format.FrameBytes;
                        long length = clip.End - clip.Start;
                        bool fadeIn = index > 0;
                        bool fadeOut = index < clips.Count - 1;
                        long position = 0;
                        while (position < length)
                        {
                            int frames = (int)Math.Min(framesPerBlock, length - position);
                            Span<byte> block = buffer.AsSpan(0, frames * format.FrameBytes);
                            take.Stream.ReadExactly(block);
                            if (fadeFrames > 0 && ((fadeIn && position < fadeFrames) ||
                                (fadeOut && length - position - frames < fadeFrames)))
                                Wave.FadeEdges(block, format, position, length, fadeFrames, fadeIn, fadeOut);
                            output.Write(block);
                            position += frames;
                        }
                    }
                    if (output.Position % 2 == 1) output.WriteByte(0);
                }
                File.Move(staging, target, overwrite: false);
            }
            finally { File.Delete(staging); }
            Console.WriteLine(JsonSerializer.Serialize(new { format, frames = total, clips },
                new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower }));
        }
        finally
        {
            foreach (var take in takes.Values) take.Stream.Dispose();
        }
    }
}
