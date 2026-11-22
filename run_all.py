"""
微观指标跟踪 — 一键运行全流程
"""
import subprocess, sys, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

STEPS = [
    ("采集数据", os.path.join("src", "fetch_data.py")),
    ("ETF资金", os.path.join("src", "fetch_nfgj.py")),
    ("分析引擎", os.path.join("src", "analyze.py")),
    ("构建页面", os.path.join("src", "build_dashboard.py")),
]


def main():
    for name, script in STEPS:
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  {name}: {script}")
        print(sep)
        result = subprocess.run([PYTHON, os.path.join(BASE_DIR, script)], cwd=BASE_DIR)
        if result.returncode != 0:
            print(f"  FAIL (exit {result.returncode})")
            sys.exit(1)
        print(f"  OK")

    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  ALL DONE!")
    print(sep)


if __name__ == "__main__":
    main()
