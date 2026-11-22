using System.Text.Json;

if (args.Length != 3 || !Uri.TryCreate(args[0], UriKind.Absolute, out var uri) ||
    (uri.Scheme != "http" && uri.Scheme != "https") || args[2].Length != 64 || !args[2].All(Uri.IsHexDigit))
{
    Console.Error.WriteLine("Usage: RangeFetch HTTP_URL OUTPUT SHA256");
    return 2;
}

using var stop = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; stop.Cancel(); };
try
{
    using var client = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false });
    var result = await Download.Run(client, uri, Path.GetFullPath(args[1]), args[2].ToLowerInvariant(), stop.Token);
    Console.WriteLine(JsonSerializer.Serialize(result, JsonSerializerOptions.Web));
    return 0;
}
catch (Exception error) when (error is IOException or HttpRequestException or UnauthorizedAccessException
                              or OperationCanceledException or InvalidDataException or ArgumentException)
{
    Console.Error.WriteLine(error.Message);
    return 1;
}
