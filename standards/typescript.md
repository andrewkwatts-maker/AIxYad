# TypeScript Expert

## Language Overview
TypeScript is a typed superset of JavaScript that compiles to plain JavaScript. Use TypeScript for web applications, Node.js backends, and any JavaScript project requiring type safety.

## Version Standards
- **Minimum**: TypeScript 5.0+
- **Target**: ES2022 or later
- **Strict Mode**: Always enabled

## Code Style

### Naming Conventions
```typescript
// PascalCase for types, interfaces, classes, enums
interface UserProfile { }
class UserService { }
enum UserRole { Admin, User }
type UserId = string;

// camelCase for variables, functions, methods
const userName = "John";
function getUserById(id: string): User { }

// UPPER_CASE for constants
const MAX_RETRIES = 3;
const API_BASE_URL = "https://api.example.com";

// Prefix interfaces with 'I' only if needed to avoid collision
interface ILogger { }  // Only if Logger class exists
```

### File Organization
```typescript
// 1. Imports (external, then internal)
import { useState, useEffect } from 'react';
import axios from 'axios';

import { UserService } from '@/services/user';
import { User } from '@/types';

// 2. Type definitions
interface Props { }

// 3. Constants
const DEFAULT_PAGE_SIZE = 20;

// 4. Component/Class/Function
export function Component() { }

// 5. Helper functions (if not extracted)
function helperFunction() { }
```

## Type System

### Prefer Interfaces for Objects
```typescript
// Good - extendable
interface User {
  id: string;
  name: string;
  email: string;
}

// Use type for unions, primitives, tuples
type UserId = string | number;
type Coordinate = [number, number];
type Status = 'pending' | 'active' | 'inactive';
```

### Use Generics
```typescript
// Generic function
function first<T>(array: T[]): T | undefined {
  return array[0];
}

// Generic interface
interface Repository<T> {
  find(id: string): Promise<T | null>;
  save(entity: T): Promise<T>;
  delete(id: string): Promise<void>;
}

// Generic class
class ApiClient<T> {
  async get(url: string): Promise<T> {
    const response = await fetch(url);
    return response.json();
  }
}
```

### Utility Types
```typescript
// Use built-in utility types
type PartialUser = Partial<User>;        // All optional
type RequiredUser = Required<User>;       // All required
type ReadonlyUser = Readonly<User>;       // All readonly
type UserName = Pick<User, 'name'>;       // Subset
type UserWithoutId = Omit<User, 'id'>;    // Exclude props
type UserKeys = keyof User;               // Union of keys
```

## Best Practices

### Strict Null Checks
```typescript
// Handle null/undefined explicitly
function getUser(id: string): User | null {
  const user = users.get(id);
  return user ?? null;
}

// Use optional chaining
const userName = user?.profile?.name;

// Use nullish coalescing
const displayName = user.name ?? 'Anonymous';
```

### Const Assertions
```typescript
// Immutable object with literal types
const config = {
  api: 'https://api.example.com',
  timeout: 5000,
} as const;

// Immutable array
const roles = ['admin', 'user', 'guest'] as const;
type Role = typeof roles[number]; // 'admin' | 'user' | 'guest'
```

### Discriminated Unions
```typescript
interface SuccessResult {
  status: 'success';
  data: User;
}

interface ErrorResult {
  status: 'error';
  error: string;
}

type Result = SuccessResult | ErrorResult;

function handleResult(result: Result) {
  if (result.status === 'success') {
    // TypeScript knows result.data exists
    console.log(result.data);
  } else {
    // TypeScript knows result.error exists
    console.error(result.error);
  }
}
```

### Branded Types
```typescript
// Prevent mixing similar primitive types
type UserId = string & { readonly brand: unique symbol };
type OrderId = string & { readonly brand: unique symbol };

function createUserId(id: string): UserId {
  return id as UserId;
}

function getUser(id: UserId): User { }

// This won't compile:
// getUser(orderId);  // Error: OrderId not assignable to UserId
```

## Error Handling

### Result Type Pattern
```typescript
type Result<T, E = Error> =
  | { ok: true; value: T }
  | { ok: false; error: E };

async function fetchUser(id: string): Promise<Result<User>> {
  try {
    const user = await api.getUser(id);
    return { ok: true, value: user };
  } catch (error) {
    return { ok: false, error: error as Error };
  }
}

// Usage
const result = await fetchUser('123');
if (result.ok) {
  console.log(result.value);
} else {
  console.error(result.error);
}
```

### Custom Error Classes
```typescript
class AppError extends Error {
  constructor(
    message: string,
    public code: string,
    public statusCode: number = 500
  ) {
    super(message);
    this.name = 'AppError';
  }
}

class NotFoundError extends AppError {
  constructor(resource: string) {
    super(`${resource} not found`, 'NOT_FOUND', 404);
  }
}
```

## Testing

### Jest/Vitest Structure
```typescript
import { describe, it, expect, vi } from 'vitest';
import { UserService } from './user-service';

describe('UserService', () => {
  describe('getUser', () => {
    it('should return user when found', async () => {
      const service = new UserService(mockRepo);
      const user = await service.getUser('123');
      expect(user).toEqual(expectedUser);
    });

    it('should throw NotFoundError when user missing', async () => {
      const service = new UserService(mockRepo);
      await expect(service.getUser('999'))
        .rejects.toThrow(NotFoundError);
    });
  });
});
```

### Mocking
```typescript
import { vi } from 'vitest';

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

mockFetch.mockResolvedValueOnce({
  ok: true,
  json: () => Promise.resolve({ id: '1', name: 'Test' }),
});
```

## Project Structure
```
project/
├── src/
│   ├── index.ts
│   ├── types/
│   │   └── index.ts
│   ├── services/
│   ├── utils/
│   └── constants/
├── tests/
│   └── *.test.ts
├── tsconfig.json
├── package.json
└── README.md
```

## tsconfig.json
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noImplicitOverride": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "declaration": true,
    "outDir": "./dist",
    "rootDir": "./src"
  },
  "include": ["src/**/*"],
  "exclude": ["node_modules", "dist"]
}
```

## Tools

### Required
- **Formatter**: Prettier
- **Linter**: ESLint with @typescript-eslint
- **Testing**: Vitest or Jest
- **Build**: tsc, esbuild, or tsx

## Anti-Patterns to Avoid

1. **Using `any`**
   ```typescript
   // Bad
   function process(data: any) { }

   // Good
   function process<T>(data: T) { }
   function process(data: unknown) { }
   ```

2. **Type Assertions Without Validation**
   ```typescript
   // Bad
   const user = response as User;

   // Good
   function isUser(obj: unknown): obj is User {
     return typeof obj === 'object' && obj !== null && 'id' in obj;
   }
   if (isUser(response)) { }
   ```

3. **Non-null Assertion Overuse**
   ```typescript
   // Bad
   const name = user!.name!;

   // Good
   const name = user?.name ?? 'Unknown';
   ```
