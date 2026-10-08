import compileall
import re
import sys

def main():
    rx = re.compile(r"[/\\](\.venv|\.venv-test|venv|env|site-packages|__pycache__|\.pytest_cache|\.git|node_modules|build|dist)[/\\]")
    success = compileall.compile_dir(".", maxlevels=100, rx=rx, quiet=1)
    if not success:
        sys.exit(1)

if __name__ == "__main__":
    main()
