#!/bin/bash
#====== Automatica/scripts/generate-docs.sh ======
# Auto-generates ProjectStructure.md and ClassStructure.md
# Usage: ./generate-docs.sh [--structure] [--classes] [--all]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse arguments
GEN_STRUCTURE=false
GEN_CLASSES=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --structure)
            GEN_STRUCTURE=true
            shift
            ;;
        --classes)
            GEN_CLASSES=true
            shift
            ;;
        --all)
            GEN_STRUCTURE=true
            GEN_CLASSES=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--structure] [--classes] [--all]"
            echo "  --structure  Generate ProjectStructure.md"
            echo "  --classes    Generate ClassStructure.md"
            echo "  --all        Generate all documentation"
            exit 0
            ;;
        *)
            shift
            ;;
    esac
done

# Default to all
if [ "$GEN_STRUCTURE" == "false" ] && [ "$GEN_CLASSES" == "false" ]; then
    GEN_STRUCTURE=true
    GEN_CLASSES=true
fi

cd "$PROJECT_ROOT"

# Generate ProjectStructure.md
if [ "$GEN_STRUCTURE" == "true" ]; then
    echo "Generating ProjectStructure.md..."

    cat > ProjectStructure.md << 'HEADER'
# Project Structure

Auto-generated project file structure.

```
HEADER

    # Generate tree structure
    if command -v tree &> /dev/null; then
        tree -I 'node_modules|.git|vendor|__pycache__|*.pyc|temp|logs' --dirsfirst -L 4 >> ProjectStructure.md
    else
        # Fallback to find
        find . -type f \
            ! -path './node_modules/*' \
            ! -path './.git/*' \
            ! -path './vendor/*' \
            ! -path './temp/*' \
            ! -path './logs/*' \
            ! -name '*.pyc' \
            -printf '%P\n' | sort | head -100 >> ProjectStructure.md
    fi

    echo '```' >> ProjectStructure.md
    echo "" >> ProjectStructure.md
    echo "Generated: $(date '+%Y-%m-%d %H:%M:%S')" >> ProjectStructure.md

    echo "  Created: ProjectStructure.md"
fi

# Generate ClassStructure.md
if [ "$GEN_CLASSES" == "true" ]; then
    echo "Generating ClassStructure.md..."

    cat > ClassStructure.md << 'HEADER'
# Class Structure

Auto-generated class, struct, and enum definitions.

HEADER

    # JavaScript/TypeScript classes
    echo "## JavaScript/TypeScript" >> ClassStructure.md
    echo "" >> ClassStructure.md

    find . -type f \( -name "*.js" -o -name "*.ts" -o -name "*.tsx" \) \
        ! -path './node_modules/*' ! -path './.git/*' 2>/dev/null | while read -r file; do
        CLASSES=$(grep -n "^class \|^export class \|^interface \|^export interface \|^type \|^export type \|^enum \|^export enum " "$file" 2>/dev/null | head -10)
        if [ -n "$CLASSES" ]; then
            echo "### $(basename "$file")" >> ClassStructure.md
            echo '```typescript' >> ClassStructure.md
            echo "$CLASSES" | sed 's/^[0-9]*:/Line /' >> ClassStructure.md
            echo '```' >> ClassStructure.md
            echo "" >> ClassStructure.md
        fi
    done

    # Python classes
    echo "## Python" >> ClassStructure.md
    echo "" >> ClassStructure.md

    find . -type f -name "*.py" \
        ! -path './node_modules/*' ! -path './.git/*' ! -path './*venv*/*' 2>/dev/null | while read -r file; do
        CLASSES=$(grep -n "^class " "$file" 2>/dev/null | head -10)
        if [ -n "$CLASSES" ]; then
            echo "### $(basename "$file")" >> ClassStructure.md
            echo '```python' >> ClassStructure.md
            echo "$CLASSES" | sed 's/^[0-9]*:/Line /' >> ClassStructure.md
            echo '```' >> ClassStructure.md
            echo "" >> ClassStructure.md
        fi
    done

    # Rust structs and enums
    echo "## Rust" >> ClassStructure.md
    echo "" >> ClassStructure.md

    find . -type f -name "*.rs" ! -path './target/*' 2>/dev/null | while read -r file; do
        STRUCTS=$(grep -n "^pub struct \|^struct \|^pub enum \|^enum \|^pub trait \|^trait " "$file" 2>/dev/null | head -10)
        if [ -n "$STRUCTS" ]; then
            echo "### $(basename "$file")" >> ClassStructure.md
            echo '```rust' >> ClassStructure.md
            echo "$STRUCTS" | sed 's/^[0-9]*:/Line /' >> ClassStructure.md
            echo '```' >> ClassStructure.md
            echo "" >> ClassStructure.md
        fi
    done

    # Go structs and interfaces
    echo "## Go" >> ClassStructure.md
    echo "" >> ClassStructure.md

    find . -type f -name "*.go" ! -path './vendor/*' 2>/dev/null | while read -r file; do
        STRUCTS=$(grep -n "^type .* struct\|^type .* interface" "$file" 2>/dev/null | head -10)
        if [ -n "$STRUCTS" ]; then
            echo "### $(basename "$file")" >> ClassStructure.md
            echo '```go' >> ClassStructure.md
            echo "$STRUCTS" | sed 's/^[0-9]*:/Line /' >> ClassStructure.md
            echo '```' >> ClassStructure.md
            echo "" >> ClassStructure.md
        fi
    done

    echo "---" >> ClassStructure.md
    echo "Generated: $(date '+%Y-%m-%d %H:%M:%S')" >> ClassStructure.md

    echo "  Created: ClassStructure.md"
fi

echo ""
echo "Documentation generated successfully."
