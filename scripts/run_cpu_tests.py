import os
import sys
import subprocess
import platform

def print_env_report():
    print("RIF CPU CI VALIDATION")
    print("-" * 25)
    print(f"OS: {platform.system()} {platform.release()}")
    print(f"Python: {sys.version.split()[0]}")
    print(f"CPU Architecture: {platform.machine()}")
    
    try:
        git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        git_branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True).strip()
        print(f"Git commit: {git_commit}")
        print(f"Git branch: {git_branch}")
    except Exception:
        print("Git info: Unavailable (not a git repository or git missing)")

    print("GPU Required: NO")
    print("Live LLM Required: NO")
    print("-" * 25)

def run_checks():
    print("\n=> Running Compile Check...")
    res = subprocess.run([sys.executable, "-m", "compileall", "-q", "."])
    if res.returncode != 0:
        print("FATAL: Compile check failed.")
        sys.exit(res.returncode)
    print("Compile check passed.")

    print("\n=> Running CPU Deterministic Test Suite...")
    os.makedirs("outputs/test_reports", exist_ok=True)
    
    # Run pytest with the defaults (our pytest.ini excludes live tests)
    pytest_cmd = [sys.executable, "-m", "pytest", "-q", "--junitxml=outputs/test_reports/test-results.xml"]
    res = subprocess.run(pytest_cmd)
    
    if res.returncode != 0:
        print(f"\nFATAL: Tests failed with exit code {res.returncode}.")
        sys.exit(res.returncode)
        
    print("\nAll deterministic tests PASSED.")

if __name__ == "__main__":
    print_env_report()
    run_checks()
