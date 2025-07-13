.PHONY: build clean publish test-build test-publish help

# Default target
help:
	@echo "Available targets:"
	@echo "  build         - Build the package"
	@echo "  clean         - Clean build artifacts"
	@echo "  publish       - Publish to PyPI (requires PYPI_TOKEN)"
	@echo "  test-build    - Test the built package locally"
	@echo "  test-publish  - Publish to TestPyPI (requires TESTPYPI_TOKEN)"
	@echo ""
	@echo "Usage:"
	@echo "  make build"
	@echo "  PYPI_TOKEN=your_token make publish"

# Clean build artifacts
clean:
	rm -rf dist/ build/ *.egg-info/

# Build the package
build: clean
	uv build

# Test the built package locally
test-build: build
	@echo "Testing package import..."
	uv run --with ./dist/taskmux-*.whl --no-project -- python -c "import taskmux; print('✅ Package import successful')"

# Publish to PyPI
publish: build
	@if [ -z "$(PYPI_TOKEN)" ]; then \
		echo "❌ Error: PYPI_TOKEN environment variable is required"; \
		echo "Usage: PYPI_TOKEN=your_token make publish"; \
		exit 1; \
	fi
	@echo "🚀 Publishing to PyPI..."
	uv publish --token $(PYPI_TOKEN)
	@echo "✅ Published successfully!"

# Publish to TestPyPI for testing
test-publish: build
	@if [ -z "$(TESTPYPI_TOKEN)" ]; then \
		echo "❌ Error: TESTPYPI_TOKEN environment variable is required"; \
		echo "Usage: TESTPYPI_TOKEN=your_token make test-publish"; \
		exit 1; \
	fi
	@echo "🧪 Publishing to TestPyPI..."
	uv publish --publish-url https://test.pypi.org/legacy/ --token $(TESTPYPI_TOKEN)
	@echo "✅ Published to TestPyPI successfully!"