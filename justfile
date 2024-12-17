uv := `which uv`

project_dir := justfile_directory()

src := project_dir / "src"
tests := project_dir / "tests"
lib := project_dir / "lib/charms/filesystem_client"
all := src + " " + tests + " " + lib

export PYTHONPATH := project_dir + ":" + project_dir / "lib" + ":" + src
export PY_COLORS := "1"
export PYTHONBREAKPOINT := "pdb.set_trace"

uv_run := "uv run --frozen --extra dev"

# Regenerate uv.lock
lock:
    uv lock --no-cache

# Upgrade uv.lock with the latest deps
upgrade:
    uv lock --upgrade --no-cache

# Generate requirements.txt from pyproject.toml
requirements: lock
    uv export --frozen --no-hashes --format=requirements-txt -o requirements.txt

# Apply coding style standards to code
fmt: lock
    echo {{PYTHONPATH}}
    {{uv_run}} ruff format {{all}}
    {{uv_run}} ruff check --fix {{all}}

# Check code against coding style standards
lint: lock
    {{uv_run}} codespell {{lib}}
    {{uv_run}} codespell {{project_dir}}
    {{uv_run}} ruff check {{all}}
    {{uv_run}} ruff format --check --diff {{all}}

# Run static type checks
typecheck *args: lock
    {{uv_run}} pyright {{args}}

# Run unit tests
unit *args: lock
    {{uv_run}} coverage run \
        --source={{src}} \
        --source={{lib}} \
        -m pytest \
        --tb native \
        -v \
        -s \
        {{args}} \
        {{tests}}/unit
    {{uv_run}} coverage report

# Run integration tests
integration *args: lock
    {{uv_run}} pytest \
        -v \
        -s \
        --tb native \
        --log-cli-level=INFO \
        {{args}} \
        {{tests}}/integration