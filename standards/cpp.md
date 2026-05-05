# C/C++ Expert

## Language Overview
C and C++ are systems programming languages offering low-level memory control and high performance. Use for operating systems, embedded systems, game engines, performance-critical applications, and hardware interfaces.

## Version Standards
- **C**: C17 (ISO/IEC 9899:2018) minimum
- **C++**: C++17 minimum, C++20 recommended
- **Compiler**: GCC 10+, Clang 12+, MSVC 2019+

## Code Style

### Naming Conventions
```cpp
// Classes/Structs: PascalCase with project prefix
class XXUserService {};
struct XXConfig {};

// Functions: camelCase or snake_case (be consistent)
void processData();
void process_data();  // Alternative style

// Variables: camelCase or snake_case
int itemCount;
int item_count;

// Constants and Macros: UPPER_SNAKE_CASE
const int MAX_BUFFER_SIZE = 1024;
#define XX_VERSION "1.0.0"

// Member variables: prefix with m_ or suffix with _
class MyClass {
    int m_count;      // Prefix style
    int count_;       // Suffix style
};

// Pointers: * attached to type
int* ptr;           // Preferred
int *ptr;           // Also acceptable
```

### File Organization
```cpp
// header.hpp
#pragma once  // Or include guards

// System includes first
#include <string>
#include <vector>
#include <memory>

// Third-party includes
#include <boost/optional.hpp>

// Project includes
#include "xx/config.hpp"
#include "xx/types.hpp"

namespace xx {

class XXUserService {
public:
    // Constructors
    XXUserService();
    explicit XXUserService(const XXConfig& config);
    ~XXUserService();

    // Disable copy
    XXUserService(const XXUserService&) = delete;
    XXUserService& operator=(const XXUserService&) = delete;

    // Enable move
    XXUserService(XXUserService&&) noexcept = default;
    XXUserService& operator=(XXUserService&&) noexcept = default;

    // Public methods
    [[nodiscard]] User* findById(const std::string& id);

private:
    // Private members
    std::unique_ptr<Impl> m_impl;  // PIMPL pattern
};

} // namespace xx
```

## Modern C++ Features

### Smart Pointers
```cpp
#include <memory>

// Unique ownership
std::unique_ptr<User> user = std::make_unique<User>("John");

// Shared ownership
std::shared_ptr<Config> config = std::make_shared<Config>();

// Weak reference (non-owning)
std::weak_ptr<Config> weakConfig = config;

// Never use raw new/delete
// Bad
User* user = new User();
delete user;

// Good
auto user = std::make_unique<User>();
// Automatically deleted when out of scope
```

### Move Semantics
```cpp
class Buffer {
public:
    Buffer(size_t size) : m_data(new char[size]), m_size(size) {}

    // Move constructor
    Buffer(Buffer&& other) noexcept
        : m_data(std::exchange(other.m_data, nullptr))
        , m_size(std::exchange(other.m_size, 0))
    {}

    // Move assignment
    Buffer& operator=(Buffer&& other) noexcept {
        if (this != &other) {
            delete[] m_data;
            m_data = std::exchange(other.m_data, nullptr);
            m_size = std::exchange(other.m_size, 0);
        }
        return *this;
    }

    ~Buffer() { delete[] m_data; }

private:
    char* m_data;
    size_t m_size;
};

// Use std::move to transfer ownership
Buffer b1(1024);
Buffer b2 = std::move(b1);  // b1 is now empty
```

### Constexpr and Compile-time
```cpp
// Compile-time constants
constexpr int BUFFER_SIZE = 1024;
constexpr double PI = 3.14159265359;

// Compile-time functions
constexpr int factorial(int n) {
    return n <= 1 ? 1 : n * factorial(n - 1);
}

static_assert(factorial(5) == 120, "Factorial calculation error");

// if constexpr (C++17)
template<typename T>
auto getValue(T t) {
    if constexpr (std::is_pointer_v<T>) {
        return *t;
    } else {
        return t;
    }
}
```

### Optional and Variant (C++17)
```cpp
#include <optional>
#include <variant>

// Optional for nullable values
std::optional<User> findUser(const std::string& id) {
    if (auto it = users.find(id); it != users.end()) {
        return it->second;
    }
    return std::nullopt;
}

// Usage
if (auto user = findUser("123")) {
    std::cout << user->name << "\n";
}

// Variant for type-safe unions
using Result = std::variant<User, Error>;

Result getUser(const std::string& id) {
    if (auto user = findUser(id)) {
        return *user;
    }
    return Error{"User not found"};
}

// Pattern matching with std::visit
std::visit([](auto&& arg) {
    using T = std::decay_t<decltype(arg)>;
    if constexpr (std::is_same_v<T, User>) {
        std::cout << "Got user: " << arg.name << "\n";
    } else {
        std::cout << "Error: " << arg.message << "\n";
    }
}, result);
```

### Ranges (C++20)
```cpp
#include <ranges>
#include <algorithm>

std::vector<int> numbers = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};

// Filter and transform
auto result = numbers
    | std::views::filter([](int n) { return n % 2 == 0; })
    | std::views::transform([](int n) { return n * n; });

// Views are lazy
for (int n : result) {
    std::cout << n << " ";  // 4 16 36 64 100
}
```

### Concepts (C++20)
```cpp
#include <concepts>

// Define a concept
template<typename T>
concept Numeric = std::integral<T> || std::floating_point<T>;

// Use in template
template<Numeric T>
T add(T a, T b) {
    return a + b;
}

// Or with requires clause
template<typename T>
    requires Numeric<T>
T multiply(T a, T b) {
    return a * b;
}
```

## Memory Management

### RAII Pattern
```cpp
class FileHandle {
public:
    explicit FileHandle(const char* filename)
        : m_file(fopen(filename, "r"))
    {
        if (!m_file) {
            throw std::runtime_error("Failed to open file");
        }
    }

    ~FileHandle() {
        if (m_file) {
            fclose(m_file);
        }
    }

    // Disable copy
    FileHandle(const FileHandle&) = delete;
    FileHandle& operator=(const FileHandle&) = delete;

    FILE* get() { return m_file; }

private:
    FILE* m_file;
};

// Usage - automatically closes file
{
    FileHandle file("data.txt");
    // Use file
}  // File closed here
```

### Avoid Raw Pointers for Ownership
```cpp
// Bad
class Container {
    Object* m_object;  // Who owns this?
public:
    ~Container() { delete m_object; }  // Easy to forget
};

// Good
class Container {
    std::unique_ptr<Object> m_object;  // Clear ownership
public:
    // Destructor handles cleanup automatically
};
```

## Error Handling

### Exceptions
```cpp
class XXException : public std::runtime_error {
public:
    explicit XXException(const std::string& message)
        : std::runtime_error(message) {}
};

class XXNotFoundError : public XXException {
public:
    explicit XXNotFoundError(const std::string& resource)
        : XXException("Not found: " + resource) {}
};

// Use noexcept where appropriate
void process() noexcept {
    // Guaranteed not to throw
}

// Exception specifications
User& getUser(const std::string& id) {
    auto it = m_users.find(id);
    if (it == m_users.end()) {
        throw XXNotFoundError(id);
    }
    return it->second;
}
```

### Expected/Result Pattern (C++23 or library)
```cpp
// Using std::expected (C++23) or tl::expected
#include <expected>

std::expected<User, Error> findUser(const std::string& id) {
    if (auto it = users.find(id); it != users.end()) {
        return it->second;
    }
    return std::unexpected(Error{"User not found"});
}

// Usage
auto result = findUser("123");
if (result) {
    std::cout << result->name << "\n";
} else {
    std::cerr << result.error().message << "\n";
}
```

## Testing

### Google Test
```cpp
#include <gtest/gtest.h>
#include "user_service.hpp"

class UserServiceTest : public ::testing::Test {
protected:
    void SetUp() override {
        service = std::make_unique<XXUserService>();
    }

    std::unique_ptr<XXUserService> service;
};

TEST_F(UserServiceTest, FindById_WhenUserExists_ReturnsUser) {
    auto user = service->findById("123");

    ASSERT_NE(user, nullptr);
    EXPECT_EQ(user->name, "John");
}

TEST_F(UserServiceTest, FindById_WhenUserNotFound_ReturnsNull) {
    auto user = service->findById("999");

    EXPECT_EQ(user, nullptr);
}

// Parameterized tests
class ValidationTest : public ::testing::TestWithParam<std::string> {};

TEST_P(ValidationTest, Validate_WithInvalidInput_ReturnsFalse) {
    EXPECT_FALSE(validate(GetParam()));
}

INSTANTIATE_TEST_SUITE_P(
    InvalidInputs,
    ValidationTest,
    ::testing::Values("", " ", "\t", "\n")
);
```

### Google Mock
```cpp
#include <gmock/gmock.h>

class MockRepository : public IRepository {
public:
    MOCK_METHOD(User*, find, (const std::string& id), (override));
    MOCK_METHOD(void, save, (const User& user), (override));
};

TEST(UserServiceTest, Create_CallsRepositorySave) {
    MockRepository mockRepo;
    XXUserService service(&mockRepo);

    User user{"1", "John"};

    EXPECT_CALL(mockRepo, save(testing::_))
        .Times(1);

    service.create(user);
}
```

## Project Structure
```
project/
├── include/
│   └── xx/
│       ├── config.hpp
│       ├── types.hpp
│       └── user_service.hpp
├── src/
│   ├── config.cpp
│   └── user_service.cpp
├── tests/
│   ├── user_service_test.cpp
│   └── CMakeLists.txt
├── CMakeLists.txt
├── conanfile.txt (or vcpkg.json)
└── README.md
```

## CMakeLists.txt
```cmake
cmake_minimum_required(VERSION 3.20)
project(MyProject VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Compiler warnings
add_compile_options(
    -Wall -Wextra -Wpedantic -Werror
    -Wno-unused-parameter
)

# Library
add_library(mylib
    src/config.cpp
    src/user_service.cpp
)
target_include_directories(mylib PUBLIC include)

# Executable
add_executable(myapp src/main.cpp)
target_link_libraries(myapp PRIVATE mylib)

# Tests
enable_testing()
find_package(GTest REQUIRED)

add_executable(mylib_tests tests/user_service_test.cpp)
target_link_libraries(mylib_tests PRIVATE mylib GTest::gtest_main)

include(GoogleTest)
gtest_discover_tests(mylib_tests)
```

## Tools

### Required
- **Build**: CMake 3.20+
- **Package Manager**: Conan or vcpkg
- **Testing**: Google Test
- **Formatting**: clang-format
- **Static Analysis**: clang-tidy, cppcheck

### .clang-format
```yaml
BasedOnStyle: Google
IndentWidth: 4
ColumnLimit: 100
PointerAlignment: Left
```

## Anti-Patterns to Avoid

1. **Raw new/delete**
   ```cpp
   // Bad
   User* user = new User();
   delete user;

   // Good
   auto user = std::make_unique<User>();
   ```

2. **C-style Casts**
   ```cpp
   // Bad
   int x = (int)someDouble;

   // Good
   int x = static_cast<int>(someDouble);
   ```

3. **Using namespace std**
   ```cpp
   // Bad (in headers especially)
   using namespace std;

   // Good
   using std::string;
   using std::vector;
   // Or fully qualify: std::string
   ```

4. **Uninitialized Variables**
   ```cpp
   // Bad
   int count;  // Uninitialized

   // Good
   int count = 0;
   int count{};  // Value initialization
   ```
