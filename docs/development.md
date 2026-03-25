# Development of FineCode

## Documentation Structure

The FineCode documentation is organized into the following sections:

### User-Facing Documentation
- **Home** (`index.md`): Landing page highlighting main benefits and quick start
- **Getting Started** (`getting-started.md`): Installation and basic usage
- **IDE and MCP Setup** (`getting-started-ide-mcp.md`): VSCode and MCP-client integration setup
- **Concepts** (`concepts.md`): Core concepts and architecture overview
- **Configuration** (`configuration.md`): Detailed configuration options
- **CLI Reference** (`cli.md`): Command-line interface documentation

### Extension Development
- **Guides**:
  - Creating an Extension (`guides/creating-extension.md`)
  - Creating a Preset (`guides/creating-preset.md`)
  - Multi-Project Workspace (`guides/workspace.md`)

### Reference
- **Built-in Actions** (`reference/actions.md`)
- **Extensions** (`reference/extensions.md`)
- **LSP and MCP Architecture** (`reference/lsp-mcp-architecture.md`): Protocol and server lifecycle internals
- **LSP Client Protocol** (`reference/lsp-protocol.md`): LSP client/server protocol details and custom commands

### Developer Documentation
- **Overview** (`development.md`): Contributing to FineCode core development
- **WM Protocol** (`wm-protocol.md`): Technical protocol and endpoint reference
- **WM-ER Protocol** (`wm-er-protocol.md`): WM and Extension Runner protocol details
- **Developing FineCode** (`guides/developing-finecode.md`): Monorepo workflows and conventions

### Potential Additions
- **F.A.Q.**: Common questions and troubleshooting
- **Changelog/Release Notes**: Version history and migration guides

## Development Environment Setup

FineCode is a monorepo containing multiple Python packages. To set up the development environment:

### Prerequisites
- Python 3.11 - 3.14
- Git

### Clone and Setup
```bash
git clone https://github.com/finecode-dev/finecode.git
cd finecode

# Create development virtual environment
python -m venv .venvs/dev_workspace
source .venvs/dev_workspace/bin/activate

# Install development dependencies
pip install --group=dev_workspace
```

### Prepare Development Environments
```bash
# Prepare virtual environments for all packages
python -m finecode prepare-envs
```

## Project Architecture

FineCode follows a modular architecture with clear separation of concerns:

### Core Components

#### Workspace Manager (`finecode/`)
The main package that:
- Discovers projects in the workspace
- Resolves configuration from multiple sources
- Manages virtual environments per tool
- Provides CLI interface
- Exposes LSP API for IDE integration
- Delegates tool execution to Extension Runners

#### Extension Runner (`finecode_extension_runner/`)
Executes tool handlers in purpose-specific virtual environments:
- Runs inside a purpose-specific venv (e.g. `dev_no_runtime`)
- Imports and executes handler code
- Communicates with Workspace Manager via JSON-RPC/LSP

#### Extension API (`finecode_extension_api/`)
Public API for extension authors:
- Defines action interfaces and base classes
- Provides built-in action definitions (lint, format, build, etc.)
- Protocol definitions for handlers and services

### Architecture Constraints
The Workspace Manager follows strict layered architecture enforced by import-linter:

```
finecode.lsp_server.lsp_server   ← top layer (IDE-facing)
        ↓
finecode.lsp_server.services     ← service layer
        ↓
finecode.domain                  ← domain models (no upward imports)
```

LSP protocol types may only be used in `finecode.runner.runner_client` and `finecode.lsp_server.lsp_server`.

## Building and Testing

### Running Tests
```bash
# Run all tests
pytest tests/

# Run tests for specific package
pytest finecode_extension_api/tests/
```

### Building Packages
```bash
# Build all packages
python -m build

# Or use the finecode build action
python -m finecode run build_artifact
```

### Development Workflow
```bash
# Run linting
python -m finecode run lint

# Check formatting
python -m finecode run check_formatting

# Format code
python -m finecode run format
```

### Running in Development Mode
```bash
# Start LSP server for IDE integration testing
python -m finecode start-lsp --stdio
```

## Contributing Guidelines

### Code Style
- Follow PEP 8 style guidelines
- Use type hints for all function parameters and return values
- Write docstrings in Google format
- Keep line length under 88 characters (Black default)

### Pull Request Process
1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes
4. Run tests and linting: `python -m finecode run lint check_formatting`
5. Submit a pull request with a clear description

### Commit Messages
Use conventional commit format:
- `feat:` for new features
- `fix:` for bug fixes
- `docs:` for documentation changes
- `refactor:` for code refactoring
- `test:` for test additions/modifications

### Testing Requirements
- All new code must include unit tests
- Maintain or improve code coverage
- Test both success and error paths
- Use descriptive test names

## Release Process

### Version Management
FineCode uses setuptools-scm for automatic versioning from git tags.

### Release Steps
1. Update version in git tag: `git tag v0.4.0`
2. Push tag: `git push origin v0.4.0`
3. CI/CD will automatically build and publish packages

### Package Dependencies
The monorepo contains interdependent packages that must be released in order:
1. `finecode_jsonrpc`
2. `finecode_httpclient`
3. `finecode_extension_api`
4. `finecode_extension_runner`
5. `finecode_builtin_handlers`
6. `finecode` (main package)

### Pre-release Versions
Use alpha/beta/rc suffixes for pre-releases:
- `v0.4.0a1` (alpha 1)
- `v0.4.0b2` (beta 2)
- `v0.4.0rc1` (release candidate 1)
