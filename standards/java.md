# Java Expert

## Language Overview
Java is a class-based, object-oriented language designed for portability and enterprise applications. Use Java for enterprise systems, Android apps, microservices, and large-scale applications.

## Version Standards
- **Minimum**: Java 17 LTS
- **Recommended**: Java 21 LTS (latest LTS)
- **Preview Features**: Avoid in production

## Code Style

### Naming Conventions
```java
// PascalCase for classes and interfaces
public class UserService {}
public interface Repository<T> {}

// camelCase for methods and variables
public void processData() {}
private String userName;

// UPPER_SNAKE_CASE for constants
public static final int MAX_CONNECTIONS = 100;

// Package names: lowercase, reverse domain
package com.example.myapp.service;
```

### Class Organization
```java
public class UserService {
    // 1. Static fields
    private static final Logger log = LoggerFactory.getLogger(UserService.class);

    // 2. Instance fields
    private final UserRepository repository;
    private final EmailService emailService;

    // 3. Constructors
    public UserService(UserRepository repository, EmailService emailService) {
        this.repository = repository;
        this.emailService = emailService;
    }

    // 4. Public methods
    public User findById(String id) {}

    // 5. Private methods
    private void validateUser(User user) {}

    // 6. Inner classes (if any)
}
```

## Modern Java Features

### Records (Java 16+)
```java
// Immutable data carriers
public record User(String id, String name, String email) {
    // Compact constructor for validation
    public User {
        Objects.requireNonNull(id, "id cannot be null");
        Objects.requireNonNull(name, "name cannot be null");
    }
}
```

### Sealed Classes (Java 17+)
```java
public sealed interface Shape permits Circle, Rectangle, Triangle {
    double area();
}

public final class Circle implements Shape {
    private final double radius;

    @Override
    public double area() {
        return Math.PI * radius * radius;
    }
}
```

### Pattern Matching
```java
// instanceof pattern matching (Java 16+)
if (obj instanceof String s) {
    System.out.println(s.length());
}

// Switch pattern matching (Java 21+)
String result = switch (shape) {
    case Circle c -> "Circle with radius " + c.radius();
    case Rectangle r -> "Rectangle " + r.width() + "x" + r.height();
    case null -> "No shape";
};
```

### Optional
```java
public Optional<User> findById(String id) {
    return Optional.ofNullable(repository.find(id));
}

// Usage
Optional<User> user = service.findById("123");
String name = user
    .map(User::name)
    .orElse("Unknown");

// Avoid
user.get(); // Bad - can throw
user.isPresent() ? user.get() : null; // Bad - defeats purpose
```

### Streams
```java
List<String> names = users.stream()
    .filter(u -> u.isActive())
    .map(User::name)
    .sorted()
    .collect(Collectors.toList());

// Parallel streams for CPU-intensive work
long count = items.parallelStream()
    .filter(this::expensiveCheck)
    .count();
```

## Exception Handling

### Custom Exceptions
```java
public class DomainException extends RuntimeException {
    private final String code;

    public DomainException(String code, String message) {
        super(message);
        this.code = code;
    }

    public DomainException(String code, String message, Throwable cause) {
        super(message, cause);
        this.code = code;
    }

    public String getCode() {
        return code;
    }
}

public class UserNotFoundException extends DomainException {
    public UserNotFoundException(String userId) {
        super("USER_NOT_FOUND", "User not found: " + userId);
    }
}
```

### Try-with-Resources
```java
// Always use for AutoCloseable resources
try (var connection = dataSource.getConnection();
     var statement = connection.prepareStatement(sql)) {
    statement.setString(1, id);
    return statement.executeQuery();
}
```

### Exception Handling Best Practices
```java
// Catch specific exceptions
try {
    processData();
} catch (IOException e) {
    log.error("Failed to process data", e);
    throw new DataProcessingException("Processing failed", e);
} catch (ValidationException e) {
    log.warn("Validation failed: {}", e.getMessage());
    return ValidationResult.failure(e.getErrors());
}

// Don't catch and ignore
try {
    process();
} catch (Exception e) {
    // Bad - silent failure
}
```

## Dependency Injection

### Constructor Injection (Preferred)
```java
@Service
public class UserService {
    private final UserRepository repository;
    private final EmailService emailService;

    // Single constructor - no @Autowired needed in Spring
    public UserService(UserRepository repository, EmailService emailService) {
        this.repository = repository;
        this.emailService = emailService;
    }
}
```

### Avoid Field Injection
```java
// Bad - hard to test
@Autowired
private UserRepository repository;

// Good - constructor injection
private final UserRepository repository;

public MyClass(UserRepository repository) {
    this.repository = repository;
}
```

## Testing

### JUnit 5 Structure
```java
import org.junit.jupiter.api.*;
import static org.assertj.core.api.Assertions.*;

class UserServiceTest {

    private UserService service;
    private UserRepository repository;

    @BeforeEach
    void setUp() {
        repository = mock(UserRepository.class);
        service = new UserService(repository);
    }

    @Test
    @DisplayName("should return user when found")
    void findById_WhenUserExists_ReturnsUser() {
        // Given
        var user = new User("1", "John", "john@example.com");
        when(repository.findById("1")).thenReturn(Optional.of(user));

        // When
        var result = service.findById("1");

        // Then
        assertThat(result).isPresent();
        assertThat(result.get().name()).isEqualTo("John");
    }

    @Test
    @DisplayName("should throw when user not found")
    void findById_WhenUserNotFound_ThrowsException() {
        when(repository.findById("999")).thenReturn(Optional.empty());

        assertThatThrownBy(() -> service.getById("999"))
            .isInstanceOf(UserNotFoundException.class)
            .hasMessageContaining("999");
    }

    @ParameterizedTest
    @ValueSource(strings = {"", " ", "  "})
    void validate_WithBlankName_ThrowsException(String name) {
        assertThatThrownBy(() -> service.validate(name))
            .isInstanceOf(ValidationException.class);
    }
}
```

### Mockito
```java
import static org.mockito.Mockito.*;

@Mock
private UserRepository repository;

@InjectMocks
private UserService service;

@Test
void shouldCallRepositorySave() {
    var user = new User("1", "John", "john@example.com");

    service.create(user);

    verify(repository).save(user);
    verify(repository, times(1)).save(any(User.class));
    verify(repository, never()).delete(any());
}
```

## Project Structure

### Maven
```
project/
├── src/
│   ├── main/
│   │   ├── java/
│   │   │   └── com/example/myapp/
│   │   │       ├── Application.java
│   │   │       ├── config/
│   │   │       ├── controller/
│   │   │       ├── service/
│   │   │       ├── repository/
│   │   │       └── model/
│   │   └── resources/
│   │       └── application.yml
│   └── test/
│       ├── java/
│       └── resources/
├── pom.xml
└── README.md
```

## pom.xml Essentials
```xml
<properties>
    <java.version>21</java.version>
    <maven.compiler.source>${java.version}</maven.compiler.source>
    <maven.compiler.target>${java.version}</maven.compiler.target>
</properties>

<dependencies>
    <!-- Testing -->
    <dependency>
        <groupId>org.junit.jupiter</groupId>
        <artifactId>junit-jupiter</artifactId>
        <scope>test</scope>
    </dependency>
    <dependency>
        <groupId>org.mockito</groupId>
        <artifactId>mockito-junit-jupiter</artifactId>
        <scope>test</scope>
    </dependency>
    <dependency>
        <groupId>org.assertj</groupId>
        <artifactId>assertj-core</artifactId>
        <scope>test</scope>
    </dependency>
</dependencies>
```

## Tools

### Required
- **Build**: Maven or Gradle
- **Testing**: JUnit 5 + Mockito + AssertJ
- **Linting**: Checkstyle, SpotBugs
- **Formatting**: Google Java Format or IDE formatter

## Anti-Patterns to Avoid

1. **Null Returns**
   ```java
   // Bad
   public User findById(String id) {
       return repository.find(id); // null if not found
   }

   // Good
   public Optional<User> findById(String id) {
       return Optional.ofNullable(repository.find(id));
   }
   ```

2. **Catching Throwable/Error**
   ```java
   // Bad
   catch (Throwable t) {}

   // Good
   catch (Exception e) {}
   ```

3. **Mutable Static State**
   ```java
   // Bad
   public static List<User> users = new ArrayList<>();

   // Good - immutable or instance-based
   public static final List<String> ROLES = List.of("ADMIN", "USER");
   ```
