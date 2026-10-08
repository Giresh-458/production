import os
import json
from pathlib import Path

def analyze_repo():
    root = Path('.')
    audit_lines = ["# REPOSITORY CLEANUP AUDIT", ""]
    
    # Classifications
    categories = {
        "__pycache__": "DELETE",
        ".pytest_cache": "DELETE",
        ".coverage": "DELETE",
        "htmlcov": "DELETE",
        "repo_scan.json": "TEMPORARY",
        "tmp_fix.py": "DELETE",
        ".venv": "IGNORE",
        "outputs": "REVIEW"
    }

    data = []
    for root_dir, dirs, files in os.walk(root):
        if ".git" in root_dir.split(os.sep) or ".venv" in root_dir.split(os.sep) or ".venv-test" in root_dir.split(os.sep):
            continue
        for file in files:
            full_path = os.path.join(root_dir, file)
            data.append({"FullName": os.path.abspath(full_path), "Length": os.path.getsize(full_path)})

    for item in data:
        if not item or not item.get("FullName"):
            continue
        full_path = item["FullName"]
        rel_path = os.path.relpath(full_path, root.resolve())
        if ".git\\" in rel_path or ".git/" in rel_path or rel_path == ".git":
            continue
            
        action = "KEEP"
        reason = "Source file"
        
        path_obj = Path(rel_path)
        parts = path_obj.parts
        name = path_obj.name
        
        # Determine classification
        if "__pycache__" in parts or name.endswith((".pyc", ".pyo")):
            action, reason = "DELETE", "Python cache"
        elif ".pytest_cache" in parts:
            action, reason = "DELETE", "Test cache"
        elif ".venv" in parts or name == ".venv":
            action, reason = "IGNORE", "Virtual environment"
        elif name.endswith((".bak", ".tmp", ".temp", ".swp")):
            action, reason = "DELETE", "Temporary file"
        elif name == "tmp_fix.py" or name == "check_prompts.py" or name == "patch_prompts.py":
            action, reason = "DELETE", "One-off script"
        elif "outputs" in parts:
            if name.endswith(".db"):
                action, reason = "DELETE", "Runtime SQLite database"
            elif name.endswith(".md") and "intermediate" in parts:
                action, reason = "DELETE", "Runtime intermediate artifacts"
            elif name.endswith(".json") and "normalized" in parts:
                action, reason = "DELETE", "Runtime normalized outputs"
            elif name.endswith(".xml") and "test_reports" in parts:
                action, reason = "DELETE", "Runtime test report"
            else:
                action, reason = "REVIEW", "Output directory file"
        elif "tests" in parts and "fixtures" in parts:
            action, reason = "KEEP", "Test fixture"
        elif "core" in parts or "agents" in parts or "ui" in parts or "scripts" in parts or "sources" in parts:
            action, reason = "KEEP", "Core project file"
        elif name.endswith(".md"):
            action, reason = "KEEP", "Documentation"
        elif name in [".gitignore", "requirements.txt", "pyproject.toml", "pytest.ini"]:
            action, reason = "KEEP", "Configuration"
            
        if "repo_scan.json" in parts:
            action, reason = "DELETE", "Temporary scan"
            
        # Large file check
        length = item.get("Length")
        size_str = ""
        if length is not None and length > 10 * 1024 * 1024:
            size_str = f" (LARGE: {length / (1024*1024):.2f} MB)"
            action, reason = "REVIEW", "Large file"
            
        audit_lines.append(f"- `{rel_path}` {size_str} -> **{action}** ({reason})")

    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)
    with open(docs_dir / "REPOSITORY_CLEANUP_AUDIT.md", "w", encoding="utf-8") as f:
        f.write("\n".join(audit_lines))

if __name__ == "__main__":
    analyze_repo()
