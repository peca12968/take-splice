using System.Buffers.Binary;
using System.Text;

internal sealed record Pcm(ushort Channels, uint SampleRate, ushort Bits)
{
    public int FrameBytes => Channels * (Bits / 8);
}

internal sealed record Wave(Pcm Format, long DataOffset, long Frames)
{
    public static void FadeEdges(Span<byte> pcm, Pcm format, long position, long length,
        uint fadeFrames, bool fadeIn, bool fadeOut)
    {
        int width = format.Bits / 8;
        for (int frame = 0; frame < pcm.Length; frame += format.FrameBytes, position++)
        {
            long distance = Math.Min(fadeIn ? position : fadeFrames,
                fadeOut ? length - position - 1 : fadeFrames);
            if (distance >= fadeFrames) continue;
            for (int channel = 0; channel < format.Channels; channel++)
            {
                Span<byte> bytes = pcm.Slice(frame + channel * width, width);
                int sample = width == 2 ? BinaryPrimitives.ReadInt16LittleEndian(bytes)
                    : bytes[0] | (bytes[1] << 8) | ((sbyte)bytes[2] << 16);
                int scaled = (int)((long)sample * distance / fadeFrames);
                bytes[0] = (byte)scaled;
                bytes[1] = (byte)(scaled >> 8);
                if (width == 3) bytes[2] = (byte)(scaled >> 16);
            }
        }
    }

    public static Wave Read(FileStream stream)
    {
        using var reader = new BinaryReader(stream, Encoding.ASCII, leaveOpen: true);
        string Tag() => Encoding.ASCII.GetString(reader.ReadBytes(4));
        if (Tag() != "RIFF") throw new InvalidDataException("expected RIFF");
        long end = 8L + reader.ReadUInt32();
        if (Tag() != "WAVE" || end != stream.Length) throw new InvalidDataException("invalid WAVE form or length");
        Pcm? format = null;
        long offset = -1, length = 0;
        while (stream.Position < end)
        {
            if (end - stream.Position < 8) throw new InvalidDataException("incomplete chunk header");
            string tag = Tag();
            uint size = reader.ReadUInt32();
            long next = stream.Position + size + (size & 1);
            if (next > end) throw new InvalidDataException("chunk extends beyond RIFF boundary");
            if (tag == "fmt ")
            {
                if (format != null || size is not (16 or 18) || reader.ReadUInt16() != 1)
                    throw new InvalidDataException("expected one uncompressed PCM format");
                ushort channels = reader.ReadUInt16();
                uint rate = reader.ReadUInt32(), byteRate = reader.ReadUInt32();
                ushort alignment = reader.ReadUInt16(), bits = reader.ReadUInt16();
                format = new Pcm(channels, rate, bits);
                if (channels is not (1 or 2) || bits is not (16 or 24) || rate == 0 ||
                    alignment != format.FrameBytes || byteRate != (long)rate * alignment ||
                    (size == 18 && reader.ReadUInt16() != 0))
                    throw new InvalidDataException("unsupported or inconsistent PCM format");
            }
            else if (tag == "data")
            {
                if (offset >= 0) throw new InvalidDataException("multiple data chunks");
                offset = stream.Position;
                length = size;
            }
            stream.Position = next;
        }
        if (format == null || offset < 0 || length % format.FrameBytes != 0)
            throw new InvalidDataException("missing format/data or incomplete PCM frame");
        return new Wave(format, offset, length / format.FrameBytes);
    }

    public static void WriteHeader(Stream output, Pcm format, long frames)
    {
        long bytes = checked(frames * format.FrameBytes);
        uint riffSize = checked((uint)(36 + bytes + (bytes & 1)));
        using var writer = new BinaryWriter(output, Encoding.ASCII, leaveOpen: true);
        writer.Write("RIFF"u8);
        writer.Write(riffSize);
        writer.Write("WAVEfmt "u8);
        writer.Write(16u);
        writer.Write((ushort)1);
        writer.Write(format.Channels);
        writer.Write(format.SampleRate);
        writer.Write(checked(format.SampleRate * (uint)format.FrameBytes));
        writer.Write((ushort)format.FrameBytes);
        writer.Write(format.Bits);
        writer.Write("data"u8);
        writer.Write(checked((uint)bytes));
    }
}
