# Go Expert

## Language Overview
Go is a statically typed, compiled language designed for simplicity, efficiency, and concurrency. Use Go for microservices, CLI tools, cloud infrastructure, and concurrent systems.

## Version Standards
- **Minimum**: Go 1.21+
- **Modules**: Always use Go modules
- **Generics**: Use where appropriate (Go 1.18+)

## Code Style

### Naming Conventions
```go
// PascalCase for exported (public)
type UserService struct {}
func ProcessData() {}
var MaxConnections = 100

// camelCase for unexported (private)
type userRepository struct {}
func processInternal() {}
var defaultTimeout = 30

// Short names for local scope
for i, v := range items {}
func (s *Server) handler(w http.ResponseWriter, r *http.Request) {}

// Acronyms are all caps
type HTTPClient struct {}
var userID string
```

### Package Organization
```go
// Package names: lowercase, single word
package user      // Good
package userService // Bad

// Avoid package name repetition
user.New()        // Good
user.NewUser()    // Redundant
```

## Code Structure

### Interface Design
```go
// Small interfaces, defined by consumer
type Reader interface {
    Read(p []byte) (n int, err error)
}

type Writer interface {
    Write(p []byte) (n int, err error)
}

// Compose interfaces
type ReadWriter interface {
    Reader
    Writer
}

// Accept interfaces, return structs
func Process(r Reader) *Result {}
```

### Struct Design
```go
type User struct {
    ID        string    `json:"id"`
    Name      string    `json:"name"`
    Email     string    `json:"email"`
    CreatedAt time.Time `json:"created_at"`
}

// Constructor function
func NewUser(name, email string) *User {
    return &User{
        ID:        uuid.New().String(),
        Name:      name,
        Email:     email,
        CreatedAt: time.Now(),
    }
}

// Methods with pointer receiver for mutation
func (u *User) UpdateEmail(email string) {
    u.Email = email
}

// Value receiver for read-only
func (u User) DisplayName() string {
    return u.Name
}
```

## Error Handling

### Error Wrapping
```go
import "fmt"

func readConfig(path string) (*Config, error) {
    data, err := os.ReadFile(path)
    if err != nil {
        return nil, fmt.Errorf("reading config %s: %w", path, err)
    }

    var config Config
    if err := json.Unmarshal(data, &config); err != nil {
        return nil, fmt.Errorf("parsing config: %w", err)
    }

    return &config, nil
}
```

### Custom Errors
```go
// Sentinel errors
var (
    ErrNotFound = errors.New("not found")
    ErrInvalidInput = errors.New("invalid input")
)

// Custom error type
type ValidationError struct {
    Field   string
    Message string
}

func (e *ValidationError) Error() string {
    return fmt.Sprintf("%s: %s", e.Field, e.Message)
}

// Error checking
if errors.Is(err, ErrNotFound) {}
var valErr *ValidationError
if errors.As(err, &valErr) {}
```

### Don't Ignore Errors
```go
// Bad
json.Unmarshal(data, &result)

// Good
if err := json.Unmarshal(data, &result); err != nil {
    return fmt.Errorf("unmarshal: %w", err)
}
```

## Concurrency

### Goroutines and Channels
```go
// Channel for communication
results := make(chan Result)

go func() {
    result := process()
    results <- result
}()

result := <-results

// Buffered channel
jobs := make(chan Job, 100)

// Close channel when done sending
close(jobs)

// Range over channel
for job := range jobs {
    process(job)
}
```

### Worker Pool Pattern
```go
func worker(id int, jobs <-chan Job, results chan<- Result) {
    for job := range jobs {
        results <- process(job)
    }
}

func main() {
    jobs := make(chan Job, 100)
    results := make(chan Result, 100)

    // Start workers
    for w := 1; w <= 3; w++ {
        go worker(w, jobs, results)
    }

    // Send jobs
    for _, job := range jobList {
        jobs <- job
    }
    close(jobs)

    // Collect results
    for range jobList {
        <-results
    }
}
```

### Context for Cancellation
```go
func processWithTimeout(ctx context.Context) error {
    ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
    defer cancel()

    select {
    case result := <-doWork():
        return handleResult(result)
    case <-ctx.Done():
        return ctx.Err()
    }
}
```

### Sync Primitives
```go
import "sync"

// Mutex for shared state
type Counter struct {
    mu    sync.Mutex
    count int
}

func (c *Counter) Increment() {
    c.mu.Lock()
    defer c.mu.Unlock()
    c.count++
}

// RWMutex for read-heavy workloads
type Cache struct {
    mu    sync.RWMutex
    items map[string]Item
}

func (c *Cache) Get(key string) (Item, bool) {
    c.mu.RLock()
    defer c.mu.RUnlock()
    item, ok := c.items[key]
    return item, ok
}

// WaitGroup for coordination
var wg sync.WaitGroup
for _, item := range items {
    wg.Add(1)
    go func(item Item) {
        defer wg.Done()
        process(item)
    }(item)
}
wg.Wait()
```

## Testing

### Table-Driven Tests
```go
func TestAdd(t *testing.T) {
    tests := []struct {
        name     string
        a, b     int
        expected int
    }{
        {"positive", 2, 3, 5},
        {"negative", -1, -2, -3},
        {"zero", 0, 0, 0},
    }

    for _, tt := range tests {
        t.Run(tt.name, func(t *testing.T) {
            result := Add(tt.a, tt.b)
            if result != tt.expected {
                t.Errorf("Add(%d, %d) = %d, want %d",
                    tt.a, tt.b, result, tt.expected)
            }
        })
    }
}
```

### Subtests and Parallel
```go
func TestUser(t *testing.T) {
    t.Run("Create", func(t *testing.T) {
        t.Parallel()
        // test code
    })

    t.Run("Update", func(t *testing.T) {
        t.Parallel()
        // test code
    })
}
```

### Mocking with Interfaces
```go
// Interface for dependency
type UserRepository interface {
    Find(id string) (*User, error)
}

// Mock implementation
type mockRepo struct {
    users map[string]*User
}

func (m *mockRepo) Find(id string) (*User, error) {
    if user, ok := m.users[id]; ok {
        return user, nil
    }
    return nil, ErrNotFound
}

// Test using mock
func TestUserService(t *testing.T) {
    repo := &mockRepo{users: map[string]*User{
        "1": {ID: "1", Name: "Test"},
    }}
    service := NewUserService(repo)

    user, err := service.GetUser("1")
    if err != nil {
        t.Fatal(err)
    }
    if user.Name != "Test" {
        t.Errorf("unexpected name: %s", user.Name)
    }
}
```

## Project Structure
```
project/
├── cmd/
│   └── myapp/
│       └── main.go
├── internal/
│   ├── config/
│   ├── handler/
│   ├── service/
│   └── repository/
├── pkg/                 # Public packages
│   └── client/
├── api/                 # API definitions
├── go.mod
├── go.sum
└── README.md
```

## Tools

### Required
- **Formatter**: gofmt / goimports
- **Linter**: golangci-lint
- **Testing**: go test
- **Documentation**: godoc

### golangci-lint Config
```yaml
# .golangci.yml
linters:
  enable:
    - errcheck
    - govet
    - staticcheck
    - unused
    - gosimple
    - ineffassign

linters-settings:
  errcheck:
    check-blank: true
```

## Anti-Patterns to Avoid

1. **Naked Returns in Long Functions**
   ```go
   // Bad (confusing in long functions)
   func process() (result int, err error) {
       // ... many lines ...
       return
   }

   // Good
   func process() (int, error) {
       return result, nil
   }
   ```

2. **Interface Pollution**
   ```go
   // Bad (too broad)
   type Doer interface {
       Do()
       Undo()
       Redo()
       Validate()
   }

   // Good (minimal)
   type Doer interface {
       Do()
   }
   ```

3. **init() Abuse**
   ```go
   // Bad (side effects, hard to test)
   func init() {
       db = connectDB()
   }

   // Good (explicit initialization)
   func main() {
       db, err := connectDB()
   }
   ```
