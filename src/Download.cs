using System.Net;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

internal sealed record Receipt(string Status, long FileBytes, long ReusedBytes, long DownloadedBytes);
internal sealed record Checkpoint(string Resource, string Digest, string ETag, long? Total);

internal static class Download
{
    public static async Task<Receipt> Run(HttpClient client, Uri uri, string destination, string digest, CancellationToken ct)
    {
        var directory = destination + ".range-fetch";
        Directory.CreateDirectory(directory);
        // A persistent lock file prevents competing processes from locking different inodes.
        using var lease = new FileStream(Path.Combine(directory, "lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        if (File.Exists(destination) && await Matches(destination, digest, ct))
        {
            var length = new FileInfo(destination).Length;
            return new Receipt("already_verified", length, length, 0);
        }

        string part = Path.Combine(directory, "data"), metadata = Path.Combine(directory, "checkpoint.json");
        string resource = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(uri.AbsoluteUri)));
        var saved = ReadCheckpoint(metadata, resource, digest);
        long offset = saved != null && File.Exists(part) ? new FileInfo(part).Length : 0;
        if (saved?.Total is long total && offset > total) offset = 0;
        long reused = offset, downloaded = 0;
        while (true)
        {
            if (offset > 0 && saved?.Total == offset)
            {
                // A previous process may have finished copying before it could publish.
                if (await Matches(part, digest, ct)) break;
                offset = reused = 0;
            }
            using var request = new HttpRequestMessage(HttpMethod.Get, uri);
            request.Headers.AcceptEncoding.ParseAdd("identity");
            if (offset > 0)
            {
                request.Headers.Range = new RangeHeaderValue(offset, null);
                request.Headers.IfRange = new RangeConditionHeaderValue(EntityTagHeaderValue.Parse(saved!.ETag));
            }
            using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, ct);
            if (offset > 0 && response.StatusCode == HttpStatusCode.RequestedRangeNotSatisfiable)
            {
                offset = reused = 0;
                continue;
            }
            if (response.Content.Headers.ContentEncoding.Any(value => !value.Equals("identity", StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException("Expected an identity-encoded representation.");

            long? segmentBytes;
            if (response.StatusCode == HttpStatusCode.OK)
            {
                if (response.Content.Headers.ContentRange != null)
                    throw new InvalidDataException("Unexpected Content-Range on a full response.");
                offset = reused = 0;
                segmentBytes = response.Content.Headers.ContentLength;
                // Truncate the old representation before installing new validator metadata.
                using (File.Create(part)) { }
                var tag = response.Headers.ETag;
                saved = tag is { IsWeak: false } && tag.Tag != "*"
                    ? new Checkpoint(resource, digest, tag.ToString(), segmentBytes) : null;
                WriteCheckpoint(metadata, saved);
            }
            else if (response.StatusCode == HttpStatusCode.PartialContent && offset > 0)
            {
                var range = response.Content.Headers.ContentRange;
                if (response.Headers.ETag?.ToString() != saved!.ETag ||
                    range?.Unit != "bytes" || range.From != offset || range.To is not long end ||
                    range.Length is not long length || end < offset || end >= length ||
                    (saved.Total.HasValue && saved.Total != length))
                    throw new InvalidDataException("Mismatched validator or Content-Range; partial response rejected.");
                segmentBytes = end - offset + 1;
                if (response.Content.Headers.ContentLength is long declared && declared != segmentBytes)
                    throw new InvalidDataException("Content-Length disagrees with Content-Range.");
                if (saved.Total == null)
                {
                    saved = saved with { Total = length };
                    WriteCheckpoint(metadata, saved);
                }
            }
            else throw new HttpRequestException($"Unexpected HTTP status {(int)response.StatusCode}.");

            long copied = await CopySegment(response.Content, part, offset, segmentBytes, ct);
            downloaded += copied;
            offset += copied;
            if (response.StatusCode == HttpStatusCode.PartialContent && offset < saved!.Total) continue;
            if (!await Matches(part, digest, ct))
            {
                File.Delete(metadata);
                File.Delete(part);
                throw new InvalidDataException("SHA-256 mismatch; staged download discarded.");
            }
            break;
        }
        File.Move(part, destination, true);
        File.Delete(metadata);
        return new Receipt("verified", new FileInfo(destination).Length, reused, downloaded);
    }

    private static Checkpoint? ReadCheckpoint(string path, string resource, string digest)
    {
        if (!File.Exists(path)) return null;
        try
        {
            var info = JsonSerializer.Deserialize<Checkpoint>(File.ReadAllText(path));
            return info != null && info.Resource == resource && info.Digest == digest &&
                info.Total is not < 0 && EntityTagHeaderValue.TryParse(info.ETag, out var tag) &&
                !tag.IsWeak && tag.Tag != "*" ? info : null;
        }
        catch (JsonException) { return null; }
    }

    private static void WriteCheckpoint(string path, Checkpoint? info)
    {
        if (info == null) { File.Delete(path); return; }
        string temp = path + ".tmp";
        File.WriteAllText(temp, JsonSerializer.Serialize(info));
        File.Move(temp, path, true);
    }

    private static async Task<long> CopySegment(HttpContent content, string path, long offset, long? expected, CancellationToken ct)
    {
        using var output = new FileStream(path, FileMode.Open, FileAccess.Write, FileShare.None);
        if (output.Length != offset) throw new InvalidDataException("Staging length changed.");
        output.Position = offset;
        using var input = await content.ReadAsStreamAsync(ct);
        var buffer = new byte[64 * 1024];
        long copied = 0;
        int count;
        while ((count = await input.ReadAsync(buffer, ct)) > 0)
        {
            if (expected.HasValue && count > expected.Value - copied)
            {
                output.SetLength(offset);
                throw new InvalidDataException("Response exceeds declared range.");
            }
            await output.WriteAsync(buffer.AsMemory(0, count), ct);
            copied += count;
        }
        if (expected.HasValue && copied != expected.Value)
            throw new EndOfStreamException("Response ended before the declared length.");
        return copied;
    }

    private static async Task<bool> Matches(string path, string digest, CancellationToken ct)
    {
        using var stream = File.OpenRead(path);
        var actual = await SHA256.HashDataAsync(stream, ct);
        return Convert.ToHexString(actual).Equals(digest, StringComparison.OrdinalIgnoreCase);
    }
}
