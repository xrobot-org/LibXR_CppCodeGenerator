import logging
import subprocess
import os

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

def get_remote_commit(url, ref='refs/heads/main'):
    result = subprocess.run(
        ['git', 'ls-remote', url, ref],
        capture_output=True,
        text=True,
        check=True
    )
    if result.stdout:
        return result.stdout.split()[0]
    raise RuntimeError("Remote ref not found")

if __name__ == "__main__":
    url = "https://github.com/xrobot-org/libxr.git"
    ref = "refs/heads/master"
    commit = get_remote_commit(url, ref)
    out_path = os.path.join(os.path.dirname(__file__), "..", "src", "libxr", "libxr_version.py")
    out_path = os.path.abspath(out_path)
    with open(out_path, "w") as f:
        f.write(f"class LibXRInfo:\n    COMMIT = '{commit}'\n")
    logging.info(f"Wrote commit {commit} to {out_path}")
