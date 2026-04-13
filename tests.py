import subprocess
import sys
from pathlib import Path

TESTDATA_DIR = Path("./testdata")
TEST_SCRIPT = Path("./pyBlock.py")
MAX_RUNTIME = 20 

# - ANSI Colors -
YELLOW = "\033[93m"
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

def remove_artifacts():
    for file in TESTDATA_DIR.glob("*.stl"):
        file.unlink()

def run_test(input_file, output_file, args):
    try:
        cmd = [sys.executable, str(TEST_SCRIPT), "--input", str(input_file), "--output", str(output_file)]
        cmd.extend(args)
        # print(cmd)
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=MAX_RUNTIME  
        )
    except subprocess.TimeoutExpired as e:
        return False, "(timeout > 20s)"

    if result.returncode != 0:
        return False, f"(exit code {result.returncode})"

    # Check 2: output file exists and is non-zero
    if not output_file.exists():
        return False, "(no output file)"

    if output_file.stat().st_size == 0:
        return False, "(empty output file)"

    return True, ""

def main():
    remove_artifacts()

    test_files = sorted([
        f for f in TESTDATA_DIR.iterdir()
        if f.is_file() and f.suffix.lower() != ".stl"
    ])

    total = 0
    passed = 0
    failed = 0

    for file in test_files:

        for height in [-1.0, 1.0]:
            for vectorizer in [""] if file.suffix.lower() == '.svg' else ["potrace", "vtracer"]:        
                total += 1
                
                ext = ""
                args = []
                if vectorizer:
                    args += ["--vectorizer", vectorizer]
                    ext = vectorizer + "."
                args += ["--relief-height", str(height)]

                output_file = file.with_suffix(f".{"relief" if height>0 else "emboss"}.{ext}stl")

                ok, reason = run_test(file, output_file, args)

                msg = str(file) + f" {height:+}mm " + (f" (vectorizer: {vectorizer})" if vectorizer else "")

                if ok:
                    passed += 1
                    print(f"{GREEN}[PASS]{RESET} {msg}")
                else:
                    failed += 1
                    print(f"{RED}[FAIL]{RESET} {msg}: {reason}")


    print(f"")    
    print(f"{GREEN}[PASS] {passed}{RESET}")
    print(f"{RED}[FAIL] {failed}{RESET}")
    print(f"Total: {total}")
    
if __name__ == "__main__":
    main()