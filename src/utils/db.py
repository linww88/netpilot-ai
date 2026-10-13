import sqlite3
import logging
import os

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_path="./data/ensp_ai.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        db_dir = os.path.dirname(self.db_path)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    type TEXT NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    user_input TEXT,
                    generated_commands TEXT,
                    status TEXT
                )
            ''')
            # 兼容旧库：为 devices 表补充元信息字段
            existing_cols = {
                row[1] for row in cursor.execute("PRAGMA table_info(devices)").fetchall()
            }
            for col, decl in (("hostname", "TEXT"), ("model", "TEXT"), ("version", "TEXT")):
                if col not in existing_cols:
                    cursor.execute(f"ALTER TABLE devices ADD COLUMN {col} {decl}")
            conn.commit()

    def add_device(self, name, ip, port, device_type, hostname="", model="", version=""):
        """添加设备记录；若同名设备已存在则更新其地址与元信息"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM devices WHERE name = ?', (name,))
            row = cursor.fetchone()
            if row:
                cursor.execute(
                    'UPDATE devices SET ip = ?, port = ?, type = ?, hostname = ?, '
                    'model = ?, version = ? WHERE id = ?',
                    (ip, port, device_type, hostname, model, version, row[0]),
                )
            else:
                cursor.execute(
                    'INSERT INTO devices (name, ip, port, type, hostname, model, version) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (name, ip, port, device_type, hostname, model, version),
                )
            conn.commit()

    def get_devices(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT name, ip, port, type FROM devices')
            return cursor.fetchall()

    def get_devices_with_meta(self):
        """返回含 hostname/model/version 的设备完整记录"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT name, ip, port, type, hostname, model, version FROM devices')
            return cursor.fetchall()

    def delete_device(self, name):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM devices WHERE name = ?', (name,))
            conn.commit()
