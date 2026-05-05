# Rust Expert

## Language Overview
Rust is a systems programming language focused on safety, concurrency, and performance. Use Rust for systems programming, CLI tools, WebAssembly, and performance-critical applications.

## Version Standards
- **Minimum**: Rust 2021 edition
- **Toolchain**: Stable (unless feature-gated nightly required)
- **MSRV**: Document Minimum Supported Rust Version

## Code Style

### Naming Conventions
```rust
// snake_case for functions, variables, modules
fn calculate_total() { }
let user_count = 42;
mod user_service;

// PascalCase for types, traits, enums
struct UserProfile { }
trait Serializable { }
enum UserRole { Admin, User }

// SCREAMING_SNAKE_CASE for constants and statics
const MAX_CONNECTIONS: u32 = 100;
static GLOBAL_CONFIG: Config = Config::new();

// Prefix with underscore for unused variables
let _unused = compute_side_effect();
```

### Module Organization
```rust
// lib.rs or main.rs
mod config;
mod error;
mod models;
mod services;

pub use config::Config;
pub use error::{Error, Result};
```

## Type System

### Prefer Strong Types
```rust
// Use newtypes for type safety
struct UserId(u64);
struct OrderId(u64);

impl UserId {
    pub fn new(id: u64) -> Self {
        Self(id)
    }
}

// Can't accidentally mix UserId and OrderId
fn get_user(id: UserId) -> User { }
```

### Enums for State
```rust
// Use enums instead of booleans for state
enum ConnectionState {
    Disconnected,
    Connecting { attempt: u32 },
    Connected { since: Instant },
    Error { message: String },
}

// Pattern matching ensures all states handled
match state {
    ConnectionState::Disconnected => reconnect(),
    ConnectionState::Connecting { attempt } if attempt < 3 => wait(),
    ConnectionState::Connecting { .. } => give_up(),
    ConnectionState::Connected { since } => check_health(since),
    ConnectionState::Error { message } => log_error(&message),
}
```

### Generics and Traits
```rust
// Define behavior with traits
trait Repository<T> {
    fn find(&self, id: &str) -> Option<T>;
    fn save(&mut self, entity: T) -> Result<()>;
}

// Implement for concrete types
impl Repository<User> for UserRepository {
    fn find(&self, id: &str) -> Option<User> { }
    fn save(&mut self, entity: User) -> Result<()> { }
}

// Use trait bounds
fn process<T: Clone + Debug>(item: T) { }

// Or where clause for complex bounds
fn process<T, U>(item: T, other: U)
where
    T: Clone + Debug,
    U: AsRef<str>,
{ }
```

## Error Handling

### Use Result and Option
```rust
// Never panic in library code
fn divide(a: f64, b: f64) -> Result<f64, MathError> {
    if b == 0.0 {
        Err(MathError::DivisionByZero)
    } else {
        Ok(a / b)
    }
}

// Use Option for optional values
fn find_user(id: &str) -> Option<User> {
    users.get(id).cloned()
}
```

### Custom Error Types
```rust
use thiserror::Error;

#[derive(Error, Debug)]
pub enum AppError {
    #[error("User not found: {0}")]
    NotFound(String),

    #[error("Invalid input: {field} - {message}")]
    Validation { field: String, message: String },

    #[error("Database error")]
    Database(#[from] sqlx::Error),

    #[error("IO error")]
    Io(#[from] std::io::Error),
}

pub type Result<T> = std::result::Result<T, AppError>;
```

### Error Propagation
```rust
// Use ? operator for propagation
fn read_config() -> Result<Config> {
    let content = std::fs::read_to_string("config.toml")?;
    let config: Config = toml::from_str(&content)?;
    Ok(config)
}

// Add context with anyhow or custom wrapper
use anyhow::{Context, Result};

fn read_config() -> Result<Config> {
    let content = std::fs::read_to_string("config.toml")
        .context("Failed to read config file")?;
    let config: Config = toml::from_str(&content)
        .context("Failed to parse config")?;
    Ok(config)
}
```

## Ownership and Borrowing

### Prefer Borrowing
```rust
// Borrow when you don't need ownership
fn process(data: &[u8]) { }          // Immutable borrow
fn modify(data: &mut Vec<u8>) { }    // Mutable borrow

// Take ownership only when necessary
fn consume(data: Vec<u8>) { }        // Takes ownership

// Use Cow for flexibility
use std::borrow::Cow;
fn process(data: Cow<'_, str>) { }
```

### Lifetime Annotations
```rust
// Explicit lifetimes when needed
fn longest<'a>(x: &'a str, y: &'a str) -> &'a str {
    if x.len() > y.len() { x } else { y }
}

// Struct with references
struct Parser<'a> {
    input: &'a str,
    position: usize,
}

impl<'a> Parser<'a> {
    fn new(input: &'a str) -> Self {
        Self { input, position: 0 }
    }
}
```

## Concurrency

### Use Channels
```rust
use std::sync::mpsc;
use std::thread;

let (tx, rx) = mpsc::channel();

thread::spawn(move || {
    tx.send(compute_result()).unwrap();
});

let result = rx.recv().unwrap();
```

### Shared State with Arc/Mutex
```rust
use std::sync::{Arc, Mutex};

let counter = Arc::new(Mutex::new(0));

let handles: Vec<_> = (0..10)
    .map(|_| {
        let counter = Arc::clone(&counter);
        thread::spawn(move || {
            let mut num = counter.lock().unwrap();
            *num += 1;
        })
    })
    .collect();

for handle in handles {
    handle.join().unwrap();
}
```

### Async/Await
```rust
use tokio;

#[tokio::main]
async fn main() {
    let result = fetch_data().await;
}

async fn fetch_data() -> Result<Data> {
    let response = reqwest::get("https://api.example.com")
        .await?
        .json()
        .await?;
    Ok(response)
}
```

## Testing

### Unit Tests
```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_add() {
        assert_eq!(add(2, 2), 4);
    }

    #[test]
    fn test_error_case() {
        let result = divide(1.0, 0.0);
        assert!(matches!(result, Err(MathError::DivisionByZero)));
    }

    #[test]
    #[should_panic(expected = "index out of bounds")]
    fn test_panic() {
        let v = vec![1, 2, 3];
        let _ = v[10];
    }
}
```

### Integration Tests
```rust
// tests/integration_test.rs
use my_crate::Config;

#[test]
fn test_full_workflow() {
    let config = Config::load("test_config.toml").unwrap();
    let result = process_with_config(&config);
    assert!(result.is_ok());
}
```

## Project Structure
```
project/
├── src/
│   ├── lib.rs           # Library root
│   ├── main.rs          # Binary entry (optional)
│   ├── error.rs
│   ├── config.rs
│   └── models/
│       ├── mod.rs
│       └── user.rs
├── tests/
│   └── integration.rs
├── benches/
│   └── benchmark.rs
├── Cargo.toml
└── README.md
```

## Cargo.toml
```toml
[package]
name = "my-project"
version = "0.1.0"
edition = "2021"
rust-version = "1.70"

[dependencies]
serde = { version = "1.0", features = ["derive"] }
tokio = { version = "1", features = ["full"] }
thiserror = "1.0"
anyhow = "1.0"

[dev-dependencies]
criterion = "0.5"

[[bench]]
name = "benchmark"
harness = false

[profile.release]
lto = true
codegen-units = 1
```

## Tools

### Required
- **Formatter**: rustfmt
- **Linter**: clippy
- **Testing**: cargo test
- **Documentation**: cargo doc

### Clippy Configuration
```toml
# clippy.toml or .clippy.toml
cognitive-complexity-threshold = 25

# In code for specific allows
#![allow(clippy::too_many_arguments)]
```

## Anti-Patterns to Avoid

1. **Unwrap in Production Code**
   ```rust
   // Bad
   let value = option.unwrap();

   // Good
   let value = option.ok_or(Error::NotFound)?;
   let value = option.unwrap_or_default();
   ```

2. **Clone to Satisfy Borrow Checker**
   ```rust
   // Bad (unnecessary clone)
   let data = expensive_data.clone();

   // Good (restructure to avoid clone)
   // Consider Rc, Arc, or restructuring ownership
   ```

3. **String Concatenation in Loops**
   ```rust
   // Bad
   let mut s = String::new();
   for item in items {
       s = s + &item.to_string();
   }

   // Good
   let s: String = items.iter()
       .map(|i| i.to_string())
       .collect();
   ```
