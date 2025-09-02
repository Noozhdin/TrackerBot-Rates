import sqlite3
import json
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
import asyncio
import aiosqlite
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# Путь к базе данных
ROOT_DIR = Path(__file__).parent
DATABASE_PATH = ROOT_DIR / "data" / "rates.db"

class SQLiteDatabase:
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(DATABASE_PATH)
        self.db_path = db_path
        self._ensure_data_dir()
        
    def _ensure_data_dir(self):
        """Создает директорию для базы данных если её нет"""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        
    @asynccontextmanager
    async def get_connection(self):
        """Асинхронный контекст менеджер для подключения к БД"""
        conn = await aiosqlite.connect(self.db_path)
        try:
            # Включаем поддержку foreign keys
            await conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            await conn.close()
            
    async def init_database(self):
        """Инициализация базы данных и создание таблиц"""
        async with self.get_connection() as conn:
            # Таблица настроек (заменяет MongoDB collection settings)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    id TEXT PRIMARY KEY DEFAULT 'singleton',
                    global_margin REAL DEFAULT 2.0,
                    global_margin_buy REAL DEFAULT 2.0,
                    global_margin_sell REAL DEFAULT 2.0,
                    per_ccy_margin TEXT DEFAULT '{}',
                    rub_rates TEXT DEFAULT '{}',
                    visibility TEXT DEFAULT '{}',
                    sort_order TEXT DEFAULT '[]',
                    rate_source TEXT DEFAULT 'exchangerate-api.com',
                    exchange_url TEXT DEFAULT '',
                    auto_update_rub_on_source_change BOOLEAN DEFAULT 0,
                    base_rates TEXT DEFAULT '{}',
                    base_rates_unit TEXT DEFAULT 'THB',
                    tv_mode BOOLEAN DEFAULT 0,
                    light_theme BOOLEAN DEFAULT 0,
                    view_mode TEXT DEFAULT 'table',
                    calculator_enabled BOOLEAN DEFAULT 1,
                    portrait_mode BOOLEAN DEFAULT 0,
                    frontend_settings TEXT DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Вставляем настройки по умолчанию если их нет
            await conn.execute("""
                INSERT OR IGNORE INTO settings (
                    id, 
                    global_margin, 
                    global_margin_buy, 
                    global_margin_sell,
                    per_ccy_margin,
                    rub_rates,
                    visibility,
                    sort_order,
                    rate_source,
                    exchange_url,
                    auto_update_rub_on_source_change,
                    base_rates,
                    base_rates_unit,
                    tv_mode,
                    light_theme,
                    view_mode,
                    calculator_enabled,
                    portrait_mode
                ) VALUES (
                    'singleton',
                    2.0,
                    2.0,
                    2.0,
                    '{}',
                    '{"card": {"buy": 0, "sell": 0}, "cash": {"buy": 0, "sell": 0}}',
                    '{}',
                    '[]',
                    'exchangerate-api.com',
                    '',
                    0,
                    '{}',
                    'THB',
                    0,
                    0,
                    'table',
                    1,
                    0
                )
            """)
            
            # Таблица кэша курсов (заменяет MongoDB collection rates_cache)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS rates_cache (
                    id TEXT PRIMARY KEY DEFAULT 'latest',
                    unit TEXT DEFAULT 'THB',
                    updated_at TEXT,
                    base_rates TEXT,
                    items TEXT,
                    actual_source TEXT,
                    requested_source TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица снимков курсов (заменяет MongoDB collection rate_snapshots)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS rate_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    base_rates TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Таблица изменений курсов (заменяет MongoDB collection changes)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    code TEXT,
                    kind TEXT,
                    old_value REAL,
                    new_value REAL,
                    source TEXT DEFAULT 'auto',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Добавляем колонку frontend_settings если её нет
            try:
                await conn.execute("ALTER TABLE settings ADD COLUMN frontend_settings TEXT DEFAULT '{}'")
                await conn.commit()
            except Exception:
                # Колонка уже существует или другая ошибка
                pass
            
            # Обновляем frontend_settings по умолчанию если это поле пустое
            await conn.execute("""
                UPDATE settings 
                SET frontend_settings = '{}' 
                WHERE id = 'singleton' AND (frontend_settings IS NULL OR frontend_settings = '')
            """)
            
            # Таблица проверок статуса (заменяет MongoDB collection status_checks)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS status_checks (
                    id TEXT PRIMARY KEY,
                    client_name TEXT,
                    timestamp TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Создаем индексы для производительности
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_code ON changes(code)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_timestamp ON changes(timestamp)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_changes_kind ON changes(kind)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON rate_snapshots(timestamp)")
            
            await conn.commit()
            logger.info("✅ База данных SQLite инициализирована")
            
    async def get_settings(self) -> dict:
        """Получить настройки (аналог MongoDB get_settings)"""
        async with self.get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM settings WHERE id = 'singleton'")
            row = await cursor.fetchone()
            
            if not row:
                # Создаем настройки по умолчанию
                from fx_service import DEFAULT_SETTINGS, CCY_CODES_ALL
                default_settings = DEFAULT_SETTINGS.copy()
                
                await conn.execute("""
                    INSERT INTO settings (
                        id, global_margin, global_margin_buy, global_margin_sell,
                        per_ccy_margin, rub_rates, visibility, sort_order,
                        rate_source, exchange_url, auto_update_rub_on_source_change,
                        base_rates, base_rates_unit
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    'singleton',
                    default_settings.get('globalMargin', 2.0),
                    default_settings.get('globalMarginBuy', 2.0),
                    default_settings.get('globalMarginSell', 2.0),
                    json.dumps(default_settings.get('perCcyMargin', {})),
                    json.dumps(default_settings.get('rubRates', {})),
                    json.dumps(default_settings.get('visibility', {})),
                    json.dumps(default_settings.get('sortOrder', [])),
                    default_settings.get('rateSource', 'exchangerate-api.com'),
                    default_settings.get('exchangeUrl', ''),
                    default_settings.get('autoUpdateRubOnSourceChange', False),
                    json.dumps({}),
                    'THB'
                ))
                await conn.commit()
                
                # Получаем созданные настройки
                cursor = await conn.execute("SELECT * FROM settings WHERE id = 'singleton'")
                row = await cursor.fetchone()
            
            if row:
                columns = [description[0] for description in cursor.description]
                settings = dict(zip(columns, row))
                
                # Конвертируем JSON поля обратно в объекты
                json_fields = ['per_ccy_margin', 'rub_rates', 'visibility', 'sort_order', 'base_rates', 'frontend_settings']
                for field in json_fields:
                    if field in settings and settings[field]:
                        try:
                            settings[field] = json.loads(settings[field])
                        except (json.JSONDecodeError, TypeError):
                            settings[field] = {} if field != 'sort_order' else []
                
                # Преобразуем названия полей в camelCase для совместимости
                result = {
                    'globalMargin': settings.get('global_margin', 2.0),
                    'globalMarginBuy': settings.get('global_margin_buy', 2.0),
                    'globalMarginSell': settings.get('global_margin_sell', 2.0),
                    'perCcyMargin': settings.get('per_ccy_margin', {}),
                    'rubRates': settings.get('rub_rates', {}),
                    'visibility': settings.get('visibility', {}),
                    'sortOrder': settings.get('sort_order', []),
                    'rateSource': settings.get('rate_source', 'exchangerate-api.com'),
                    'exchangeUrl': settings.get('exchange_url', ''),
                    'autoUpdateRubOnSourceChange': settings.get('auto_update_rub_on_source_change', False),
                    'baseRates': settings.get('base_rates', {}),
                    'baseRatesUnit': settings.get('base_rates_unit', 'THB'),
                    'tvMode': bool(settings.get('tv_mode', False)),
                    'lightTheme': bool(settings.get('light_theme', False)),
                    'viewMode': settings.get('view_mode', 'table'),
                    'calculatorEnabled': bool(settings.get('calculator_enabled', True)),
                    'portraitMode': bool(settings.get('portrait_mode', False)),
                    'frontendSettings': settings.get('frontend_settings', {})
                }
                
                return result
                
            return {}
    
    async def save_settings(self, patch: dict) -> dict:
        """Сохранить настройки (аналог MongoDB save_settings)"""
        async with self.get_connection() as conn:
            # Преобразуем camelCase в snake_case
            field_mapping = {
                'globalMargin': 'global_margin',
                'globalMarginBuy': 'global_margin_buy',
                'globalMarginSell': 'global_margin_sell',
                'perCcyMargin': 'per_ccy_margin',
                'rubRates': 'rub_rates',
                'sortOrder': 'sort_order',
                'rateSource': 'rate_source',
                'exchangeUrl': 'exchange_url',
                'autoUpdateRubOnSourceChange': 'auto_update_rub_on_source_change',
                'baseRates': 'base_rates',
                'baseRatesUnit': 'base_rates_unit',
                'tvMode': 'tv_mode',
                'lightTheme': 'light_theme',
                'viewMode': 'view_mode',
                'calculatorEnabled': 'calculator_enabled',
                'portraitMode': 'portrait_mode',
                'frontendSettings': 'frontend_settings'
            }
            
            update_fields = []
            update_values = []
            
            for key, value in patch.items():
                db_field = field_mapping.get(key, key)
                
                # JSON поля
                if db_field in ['per_ccy_margin', 'rub_rates', 'visibility', 'sort_order', 'base_rates', 'frontend_settings']:
                    value = json.dumps(value)
                
                update_fields.append(f"{db_field} = ?")
                update_values.append(value)
            
            if update_fields:
                update_fields.append("updated_at = CURRENT_TIMESTAMP")
                sql = f"UPDATE settings SET {', '.join(update_fields)} WHERE id = 'singleton'"
                await conn.execute(sql, update_values)
                await conn.commit()
            
            return await self.get_settings()
    
    async def get_latest_cache(self) -> Optional[dict]:
        """Получить последний кэш курсов"""
        async with self.get_connection() as conn:
            cursor = await conn.execute("SELECT * FROM rates_cache WHERE id = 'latest'")
            row = await cursor.fetchone()
            
            if row:
                columns = [description[0] for description in cursor.description]
                cache = dict(zip(columns, row))
                
                # Конвертируем JSON поля
                json_fields = ['base_rates', 'items']
                for field in json_fields:
                    if field in cache and cache[field]:
                        try:
                            cache[field] = json.loads(cache[field])
                        except (json.JSONDecodeError, TypeError):
                            cache[field] = {}
                
                # Преобразуем названия полей
                result = {
                    'unit': cache.get('unit'),
                    'updatedAt': cache.get('updated_at'),
                    'baseRates': cache.get('base_rates', {}),
                    'items': cache.get('items', []),
                    'actualSource': cache.get('actual_source'),
                    'requestedSource': cache.get('requested_source')
                }
                
                return result
                
            return None
    
    async def save_cache(self, doc: dict):
        """Сохранить кэш курсов"""
        async with self.get_connection() as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO rates_cache 
                (id, unit, updated_at, base_rates, items, actual_source, requested_source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                'latest',
                doc.get('unit', 'THB'),
                doc.get('updatedAt'),
                json.dumps(doc.get('baseRates', {})),
                json.dumps(doc.get('items', [])),
                doc.get('actualSource'),
                doc.get('requestedSource')
            ))
            await conn.commit()
    
    async def insert_snapshot(self, base_rates: dict):
        """Вставить снимок курсов"""
        async with self.get_connection() as conn:
            await conn.execute("""
                INSERT INTO rate_snapshots (timestamp, base_rates)
                VALUES (?, ?)
            """, (
                datetime.utcnow().isoformat(),
                json.dumps(base_rates)
            ))
            await conn.commit()
    
    async def record_changes(self, prev_items: List[dict], new_items: List[dict], source: str = "auto"):
        """Записать изменения курсов"""
        prev_map = {i["code"]: i for i in (prev_items or [])}
        ops = []
        
        for n in new_items:
            p = prev_map.get(n["code"])
            if not p:
                ops.append((
                    datetime.utcnow().isoformat(),
                    n["code"],
                    "buy",
                    None,
                    n["buy"],
                    source
                ))
                ops.append((
                    datetime.utcnow().isoformat(),
                    n["code"],
                    "sell",
                    None,
                    n["sell"],
                    source
                ))
                continue
            
            if round(n["buy"], 2) != round(p["buy"], 2):
                ops.append((
                    datetime.utcnow().isoformat(),
                    n["code"],
                    "buy",
                    p["buy"],
                    n["buy"],
                    source
                ))
            
            if round(n["sell"], 2) != round(p["sell"], 2):
                ops.append((
                    datetime.utcnow().isoformat(),
                    n["code"],
                    "sell",
                    p["sell"],
                    n["sell"],
                    source
                ))
        
        if ops:
            async with self.get_connection() as conn:
                await conn.executemany("""
                    INSERT INTO changes (timestamp, code, kind, old_value, new_value, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, ops)
                await conn.commit()
    
    async def get_changes(self, limit: int = 100) -> List[dict]:
        """Получить изменения курсов"""
        async with self.get_connection() as conn:
            cursor = await conn.execute("""
                SELECT timestamp, code, kind, old_value, new_value, source
                FROM changes 
                ORDER BY created_at DESC 
                LIMIT ?
            """, (limit,))
            
            rows = await cursor.fetchall()
            changes = []
            
            for row in rows:
                changes.append({
                    'ts': row[0],
                    'code': row[1],
                    'kind': row[2],
                    'old': row[3],
                    'new': row[4],
                    'source': row[5]
                })
            
            return changes
    
    async def delete_rub_changes(self, rub_type: str) -> int:
        """Удалить изменения рублевых курсов"""
        async with self.get_connection() as conn:
            cursor = await conn.execute("""
                DELETE FROM changes 
                WHERE code = 'RUB' AND kind LIKE ?
            """, (f"{rub_type}-%",))
            
            deleted_count = cursor.rowcount
            await conn.commit()
            return deleted_count
    
    async def insert_status_check(self, status_check: dict):
        """Вставить проверку статуса"""
        async with self.get_connection() as conn:
            await conn.execute("""
                INSERT INTO status_checks (id, client_name, timestamp)
                VALUES (?, ?, ?)
            """, (
                status_check["id"],
                status_check["client_name"],
                status_check["timestamp"].isoformat() if isinstance(status_check["timestamp"], datetime) else status_check["timestamp"]
            ))
            await conn.commit()
    
    async def get_status_checks(self) -> List[dict]:
        """Получить проверки статуса"""
        async with self.get_connection() as conn:
            cursor = await conn.execute("""
                SELECT id, client_name, timestamp
                FROM status_checks 
                ORDER BY created_at DESC
            """)
            
            rows = await cursor.fetchall()
            checks = []
            
            for row in rows:
                checks.append({
                    'id': row[0],
                    'client_name': row[1],
                    'timestamp': row[2]
                })
            
            return checks

# Глобальный экземпляр базы данных
db = SQLiteDatabase()
