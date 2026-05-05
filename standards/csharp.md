# C# Expert

## Language Overview
C# is a modern, object-oriented language for the .NET platform. Use C# for enterprise applications, web APIs (ASP.NET), desktop apps, games (Unity), and cloud services.

## Version Standards
- **Minimum**: C# 10 / .NET 6
- **Recommended**: C# 12 / .NET 8 (latest LTS)
- **Nullable**: Enable nullable reference types

## Code Style

### Naming Conventions
```csharp
// PascalCase for public members, types, namespaces
public class UserService { }
public interface IRepository<T> { }  // Prefix interfaces with I
public void ProcessData() { }
public string UserName { get; set; }

// camelCase for private fields (with underscore prefix)
private readonly ILogger _logger;
private string _userName;

// camelCase for parameters and local variables
public void Process(string userName) {
    var localValue = userName;
}

// UPPER_CASE for constants
public const int MaxConnections = 100;
```

### File Organization
```csharp
// File: UserService.cs
using System;
using System.Collections.Generic;
using Microsoft.Extensions.Logging;

using MyApp.Models;
using MyApp.Repositories;

namespace MyApp.Services;

public class UserService
{
    // Fields
    private readonly IUserRepository _repository;
    private readonly ILogger<UserService> _logger;

    // Constructor
    public UserService(IUserRepository repository, ILogger<UserService> logger)
    {
        _repository = repository;
        _logger = logger;
    }

    // Properties
    public bool IsEnabled { get; private set; }

    // Public methods
    public async Task<User?> GetByIdAsync(string id) { }

    // Private methods
    private void ValidateUser(User user) { }
}
```

## Modern C# Features

### Records
```csharp
// Immutable reference types
public record User(string Id, string Name, string Email);

// Record with additional members
public record User(string Id, string Name, string Email)
{
    public string DisplayName => $"{Name} <{Email}>";
}

// Record struct for value types
public readonly record struct Point(int X, int Y);
```

### Pattern Matching
```csharp
// Switch expressions
string description = shape switch
{
    Circle { Radius: > 100 } => "Large circle",
    Circle c => $"Circle with radius {c.Radius}",
    Rectangle { Width: var w, Height: var h } when w == h => "Square",
    Rectangle r => $"Rectangle {r.Width}x{r.Height}",
    null => "No shape",
    _ => "Unknown shape"
};

// Property patterns
if (user is { IsActive: true, Role: "Admin" })
{
    // User is active admin
}

// List patterns (C# 11+)
int[] numbers = { 1, 2, 3, 4, 5 };
if (numbers is [1, 2, .. var rest])
{
    // rest = { 3, 4, 5 }
}
```

### Nullable Reference Types
```csharp
// Enable in project file or globally
#nullable enable

public class UserService
{
    // Non-nullable - compiler ensures not null
    private readonly IRepository _repository;

    // Nullable - explicitly marked
    public User? FindById(string id)
    {
        return _repository.Find(id);
    }

    // Handle nullability
    public string GetUserName(string id)
    {
        var user = FindById(id);
        return user?.Name ?? "Unknown";
    }
}
```

### Primary Constructors (C# 12)
```csharp
public class UserService(IUserRepository repository, ILogger<UserService> logger)
{
    public async Task<User?> GetByIdAsync(string id)
    {
        logger.LogInformation("Getting user {Id}", id);
        return await repository.FindByIdAsync(id);
    }
}
```

### Collection Expressions (C# 12)
```csharp
// List initialization
List<int> numbers = [1, 2, 3, 4, 5];

// Spread operator
int[] first = [1, 2, 3];
int[] second = [4, 5, 6];
int[] combined = [..first, ..second]; // [1, 2, 3, 4, 5, 6]
```

## Async/Await

### Async Best Practices
```csharp
// Always use async suffix
public async Task<User> GetUserAsync(string id)
{
    return await _repository.FindByIdAsync(id);
}

// Use ConfigureAwait(false) in libraries
public async Task<User> GetUserAsync(string id)
{
    return await _repository.FindByIdAsync(id).ConfigureAwait(false);
}

// Avoid async void (except event handlers)
// Bad
public async void ProcessData() { }

// Good
public async Task ProcessDataAsync() { }

// Parallel execution
var tasks = ids.Select(id => GetUserAsync(id));
var users = await Task.WhenAll(tasks);
```

### Cancellation Tokens
```csharp
public async Task<User> GetUserAsync(
    string id,
    CancellationToken cancellationToken = default)
{
    cancellationToken.ThrowIfCancellationRequested();
    return await _repository.FindByIdAsync(id, cancellationToken);
}
```

## Error Handling

### Custom Exceptions
```csharp
public class DomainException : Exception
{
    public string Code { get; }

    public DomainException(string code, string message)
        : base(message)
    {
        Code = code;
    }

    public DomainException(string code, string message, Exception innerException)
        : base(message, innerException)
    {
        Code = code;
    }
}

public class UserNotFoundException : DomainException
{
    public UserNotFoundException(string userId)
        : base("USER_NOT_FOUND", $"User not found: {userId}")
    {
    }
}
```

### Result Pattern
```csharp
public readonly struct Result<T>
{
    public T? Value { get; }
    public string? Error { get; }
    public bool IsSuccess => Error is null;

    private Result(T value) => Value = value;
    private Result(string error) => Error = error;

    public static Result<T> Success(T value) => new(value);
    public static Result<T> Failure(string error) => new(error);

    public TResult Match<TResult>(
        Func<T, TResult> onSuccess,
        Func<string, TResult> onFailure)
        => IsSuccess ? onSuccess(Value!) : onFailure(Error!);
}

// Usage
public Result<User> GetUser(string id)
{
    var user = _repository.Find(id);
    return user is not null
        ? Result<User>.Success(user)
        : Result<User>.Failure("User not found");
}
```

## Dependency Injection

### Registration
```csharp
// Program.cs
var builder = WebApplication.CreateBuilder(args);

// Register services
builder.Services.AddScoped<IUserRepository, UserRepository>();
builder.Services.AddScoped<IUserService, UserService>();
builder.Services.AddSingleton<ICacheService, MemoryCacheService>();
builder.Services.AddTransient<IEmailSender, SmtpEmailSender>();

// Configure options
builder.Services.Configure<AppSettings>(
    builder.Configuration.GetSection("AppSettings"));

var app = builder.Build();
```

### Constructor Injection
```csharp
public class UserService : IUserService
{
    private readonly IUserRepository _repository;
    private readonly ILogger<UserService> _logger;
    private readonly AppSettings _settings;

    public UserService(
        IUserRepository repository,
        ILogger<UserService> logger,
        IOptions<AppSettings> settings)
    {
        _repository = repository;
        _logger = logger;
        _settings = settings.Value;
    }
}
```

## Testing

### xUnit Structure
```csharp
using Xunit;
using Moq;
using FluentAssertions;

public class UserServiceTests
{
    private readonly Mock<IUserRepository> _repositoryMock;
    private readonly UserService _sut;  // System Under Test

    public UserServiceTests()
    {
        _repositoryMock = new Mock<IUserRepository>();
        _sut = new UserService(_repositoryMock.Object);
    }

    [Fact]
    public async Task GetByIdAsync_WhenUserExists_ReturnsUser()
    {
        // Arrange
        var user = new User("1", "John", "john@example.com");
        _repositoryMock
            .Setup(r => r.FindByIdAsync("1"))
            .ReturnsAsync(user);

        // Act
        var result = await _sut.GetByIdAsync("1");

        // Assert
        result.Should().NotBeNull();
        result!.Name.Should().Be("John");
    }

    [Theory]
    [InlineData("")]
    [InlineData(" ")]
    [InlineData(null)]
    public async Task GetByIdAsync_WithInvalidId_ThrowsArgumentException(string? id)
    {
        // Act & Assert
        await Assert.ThrowsAsync<ArgumentException>(
            () => _sut.GetByIdAsync(id!));
    }
}
```

### Integration Testing
```csharp
public class UserApiTests : IClassFixture<WebApplicationFactory<Program>>
{
    private readonly HttpClient _client;

    public UserApiTests(WebApplicationFactory<Program> factory)
    {
        _client = factory.CreateClient();
    }

    [Fact]
    public async Task GetUser_ReturnsOk()
    {
        var response = await _client.GetAsync("/api/users/1");

        response.StatusCode.Should().Be(HttpStatusCode.OK);
    }
}
```

## Project Structure
```
Solution/
├── src/
│   ├── MyApp.Api/           # Web API project
│   ├── MyApp.Core/          # Domain models, interfaces
│   ├── MyApp.Infrastructure/ # Data access, external services
│   └── MyApp.Application/   # Business logic, use cases
├── tests/
│   ├── MyApp.UnitTests/
│   └── MyApp.IntegrationTests/
├── MyApp.sln
└── README.md
```

## .csproj Essentials
```xml
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <TreatWarningsAsErrors>true</TreatWarningsAsErrors>
  </PropertyGroup>

  <ItemGroup>
    <PackageReference Include="Microsoft.Extensions.DependencyInjection" Version="8.0.0" />
  </ItemGroup>
</Project>
```

## Tools

### Required
- **IDE**: Visual Studio 2022 or JetBrains Rider
- **Testing**: xUnit + Moq + FluentAssertions
- **Analysis**: .NET Analyzers (built-in)
- **Formatting**: dotnet format or IDE

## Anti-Patterns to Avoid

1. **Using Task.Result or .Wait()**
   ```csharp
   // Bad - can cause deadlocks
   var result = GetDataAsync().Result;

   // Good
   var result = await GetDataAsync();
   ```

2. **Not Disposing Resources**
   ```csharp
   // Bad
   var stream = File.OpenRead("file.txt");

   // Good
   using var stream = File.OpenRead("file.txt");

   // Or
   await using var stream = File.OpenRead("file.txt");
   ```

3. **Catching Exception Without Reason**
   ```csharp
   // Bad
   catch (Exception) { }

   // Good
   catch (SpecificException ex)
   {
       _logger.LogError(ex, "Operation failed");
       throw;
   }
   ```
