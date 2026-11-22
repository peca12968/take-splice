using System.Text.Json;
using System.Globalization;

int idleTimeoutMs = 30_000;
int? maxRangeBytes = null;
var seen = new HashSet<string>();
bool validOptions = args.Length >= 3 && (args.Length - 3) % 2 == 0;
for (int i = 3; validOptions && i < args.Length; i += 2)
{
    if (!seen.Add(args[i]) || !int.TryParse(args[i+1], NumberStyles.None, CultureInfo.InvariantCulture, out int value) || value <= 0)
        validOptions = false;
    else if (args[i] == "--idle-timeout-ms") idleTimeoutMs = value;
    else if (args[i] == "--max-range-bytes") maxRangeBytes = value;
    else validOptions = false;
}
if (!validOptions ||
    !Uri.TryCreate(args[0], UriKind.Absolute, out var uri) ||
    (uri.Scheme != "http" && uri.Scheme != "https") || args[2].Length != 64 || !args[2].All(Uri.IsHexDigit))
{
    Console.Error.WriteLine("Usage: RangeFetch HTTP_URL OUTPUT SHA256 [--idle-timeout-ms N] [--max-range-bytes N]");
    return 2;
}

using var stop = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop.Cancel(); };
try
{
    using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false });
    var result = await Download.Run(client, uri, Path.GetFullPath(args[1]), args[2].ToLowerInvariant(),
        idleTimeoutMs, maxRangeBytes, stop.Token);
    Console.WriteLine(JsonSerializer.Serialize(result, JsonSerializerOptions.Web));
    return 0;
}
catch (Exception error) when (error is IOException or HttpRequestException or UnauthorizedAccessException
                              or OperationCanceledException or InvalidDataException or ArgumentException or TimeoutException)
{
    Console.Error.WriteLine(error.Message);
    return 1;
}
