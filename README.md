# Rust Analyzer Harness

A streamlined rust-analyzer interface designed specifically for LLMs and AI coding assistants like Claude Code.

## Features

- **Simplified Command Interface**: Easy-to-use commands optimized for LLM interaction
- **Error-First Display**: Prioritizes errors over warnings for quick issue identification
- **Smart Filtering**: Focus on specific lines or error types
- **Clean Output**: No debug noise, clear summaries, actionable diagnostics
- **Test-Aware**: Properly analyzes code in `#[cfg(test)]` modules

## Installation

```bash
pip install rust-analyzer-harness
```

Or install from source:

```bash
git clone https://github.com/gretmn102/rust-analyzer-harness.git
cd rust-analyzer-harness
pip install -e .
```

## Usage

### Basic Commands

```bash
# Show all diagnostics in a file
ra src/main.rs

# Show only errors
ra src/main.rs --errors

# Show diagnostics around a specific line
ra src/main.rs:42

# Show all diagnostics with full details
ra src/main.rs --full

# Show all errors in the project
ra --all --errors
```

### Output Example

```
**2 errors**, 5 warnings

main.rs:10:5: **Error** - cannot find value `x` in this scope
  Error code: E0425
  Source: rustc

main.rs:15:12: **Error** - mismatched types
  Error code: E0308
  Source: rustc
  Related:
    - main.rs:15: expected `String`, found `&str`
```

## Requirements

- Python 3.8+
- rust-analyzer installed and available in PATH
- A Rust project to analyze

## Why This Tool?

When using AI coding assistants, the standard rust-analyzer LSP interface is too verbose and complex. This tool provides:

1. **Minimal Syntax**: No need to remember complex LSP commands
2. **Relevant Information**: Shows what matters most - errors first, then warnings
3. **Line-Specific Queries**: Quickly check issues around specific code locations
4. **Clean Integration**: Designed to work seamlessly with LLM tool-use patterns

## License

Free and Open Source Software (FOSS) - see LICENSE file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.