# Python Expert

## Language Overview
Python is a high-level, interpreted language emphasizing readability and simplicity. Use Python for scripting, web backends, data science, ML/AI, and automation.

## Version Standards
- **Minimum**: Python 3.9+
- **Recommended**: Python 3.11+ (performance improvements)
- **Type Hints**: Required for all public APIs

## Code Style

### PEP 8 Compliance
```python
# Naming conventions
class ClassName:          # PascalCase for classes
    def method_name(self):  # snake_case for functions/methods
        local_variable = 1   # snake_case for variables
        CONSTANT_VALUE = 42  # UPPER_CASE for constants
        _private_var = "x"   # leading underscore for private

# Line length: 88 characters (Black default) or 79 (PEP 8 strict)
```

### Import Organization
```python
# Standard library imports
import os
import sys
from typing import Optional, List

# Third-party imports
import requests
from fastapi import FastAPI

# Local imports
from myproject.utils import helper
from myproject.models import User
```

## Type Hints

### Function Signatures
```python
from typing import Optional, List, Dict, Union, TypeVar, Generic

def process_data(
    items: List[str],
    count: int = 10,
    config: Optional[Dict[str, Any]] = None
) -> List[str]:
    """Process and return filtered items."""
    ...

# Generic types
T = TypeVar('T')

def first_or_none(items: List[T]) -> Optional[T]:
    return items[0] if items else None
```

### Class Type Hints
```python
from dataclasses import dataclass
from typing import ClassVar

@dataclass
class User:
    name: str
    email: str
    age: int = 0
    _internal: ClassVar[str] = "hidden"
```

## Best Practices

### Use Context Managers
```python
# Good
with open('file.txt', 'r') as f:
    content = f.read()

# Bad
f = open('file.txt', 'r')
content = f.read()
f.close()
```

### Use List/Dict Comprehensions
```python
# Good
squares = [x**2 for x in range(10)]
filtered = {k: v for k, v in data.items() if v > 0}

# Bad (for simple cases)
squares = []
for x in range(10):
    squares.append(x**2)
```

### Use Pathlib for File Paths
```python
from pathlib import Path

# Good
config_path = Path(__file__).parent / "config" / "settings.json"

# Bad
config_path = os.path.join(os.path.dirname(__file__), "config", "settings.json")
```

### Use Dataclasses or Pydantic
```python
from dataclasses import dataclass, field
from typing import List

@dataclass
class Config:
    host: str
    port: int = 8080
    tags: List[str] = field(default_factory=list)

# Or with Pydantic for validation
from pydantic import BaseModel, validator

class Config(BaseModel):
    host: str
    port: int = 8080

    @validator('port')
    def port_range(cls, v):
        if not 1 <= v <= 65535:
            raise ValueError('Invalid port')
        return v
```

## Error Handling

### Specific Exception Handling
```python
# Good
try:
    result = api.fetch_data()
except ConnectionError as e:
    logger.error(f"Network error: {e}")
    raise
except ValueError as e:
    logger.warning(f"Invalid data: {e}")
    return default_value

# Bad
try:
    result = api.fetch_data()
except Exception:
    pass  # Silent failure
```

### Custom Exceptions
```python
class DomainError(Exception):
    """Base exception for domain errors."""
    pass

class ValidationError(DomainError):
    """Raised when validation fails."""
    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(f"{field}: {message}")
```

## Testing

### Pytest Structure
```python
# test_module.py
import pytest
from mymodule import function_to_test

class TestFunctionToTest:
    def test_normal_case(self):
        assert function_to_test("input") == "expected"

    def test_edge_case(self):
        assert function_to_test("") == ""

    def test_error_case(self):
        with pytest.raises(ValueError):
            function_to_test(None)

    @pytest.fixture
    def sample_data(self):
        return {"key": "value"}

    def test_with_fixture(self, sample_data):
        assert function_to_test(sample_data) is not None
```

### Mocking
```python
from unittest.mock import Mock, patch, MagicMock

@patch('mymodule.external_api')
def test_with_mock(mock_api):
    mock_api.fetch.return_value = {"data": "test"}
    result = my_function()
    assert result == expected
    mock_api.fetch.assert_called_once()
```

## Project Structure
```
project/
├── src/
│   └── project_name/
│       ├── __init__.py
│       ├── main.py
│       ├── models/
│       ├── services/
│       └── utils/
├── tests/
│   ├── conftest.py
│   ├── test_main.py
│   └── fixtures/
├── pyproject.toml
├── requirements.txt
└── README.md
```

## Tools

### Required
- **Formatter**: Black or Ruff
- **Linter**: Ruff or Flake8 + isort
- **Type Checker**: mypy or pyright
- **Testing**: pytest + pytest-cov

### pyproject.toml Example
```toml
[tool.black]
line-length = 88

[tool.ruff]
line-length = 88
select = ["E", "F", "I", "N", "W"]

[tool.mypy]
python_version = "3.11"
strict = true

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--cov=src --cov-report=term-missing"
```

## Anti-Patterns to Avoid

1. **Mutable Default Arguments**
   ```python
   # Bad
   def func(items=[]):
       items.append(1)
       return items

   # Good
   def func(items=None):
       items = items or []
       items.append(1)
       return items
   ```

2. **Bare Except**
   ```python
   # Bad
   except:
       pass

   # Good
   except Exception as e:
       logger.exception("Error occurred")
   ```

3. **Using `type()` for Type Checking**
   ```python
   # Bad
   if type(x) == list:

   # Good
   if isinstance(x, list):
   ```
