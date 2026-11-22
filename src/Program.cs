using System.Text.Json;
using System.Globalization;

int idleTimeoutMs = 30_000;
if (args.Length is not (3 or 5) ||
    (args.Length == 5 && (args[3] != "--idle-timeout-ms" ||
        !int.TryParse(args[4], NumberStyles.None, CultureInfo.InvariantCulture, out idleTimeoutMs) ||
        idleTimeoutMs <= 0)) ||
    !Uri.TryCreate(args[0], UriKind.Absolute, out var uri) ||
    (uri.Scheme != "http" && uri.Scheme != "https") || args[2].Length != 64 || !args[2].All(Uri.IsHexDigit))
{
    Console.Error.WriteLine("Usage: RangeFetch HTTP_URL OUTPUT SHA256 [--idle-timeout-ms POSITIVE_INTEGER]");
    return 2;
}

using var stop = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop.Cancel(); };
try
{
    using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false });
    var result = await Download.Run(client, uri, Path.GetFullPath(args[1]), args[2].ToLowerInvariant(),
        idleTimeoutMs, stop.Token);
    Console.WriteLine(JsonSerializer.Serialize(result, JsonSerializerOptions.Web));
    return 0;
}
catch (Exception error) when (error is IOException or HttpRequestException or UnauthorizedAccessException
                              or OperationCanceledException or InvalidDataException or ArgumentException or TimeoutException)
{
    Console.Error.WriteLine(error.Message);
    return 1;
}
