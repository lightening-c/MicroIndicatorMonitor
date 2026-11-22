"""集中定义项目路径，供各脚本统一引用。"""
import os

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
ASSETS_DIR = os.path.join(ROOT_DIR, "assets")
EXPORT_DIR = os.path.join(ROOT_DIR, "exports")
CONFIG_FILE = os.path.join(ROOT_DIR, "config.yaml")
