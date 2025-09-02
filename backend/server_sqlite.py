from fastapi import FastAPI, APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Dict, Optional
import uuid
from datetime import datetime, timedelta
from fastapi.concurrency import run_in_threadpool
from fastapi import Query
import json

# Импортируем нашу новую базу данных
from database import db
from fx_service import DEFAULT_SETTINGS, fetch_base_rates_rub, compute_buy_sell, build_public_payload, DENOMS, RATE_SOURCES, get_real_24h_history
from history24_sqlite import router as history_router

ROOT_DIR = Path(__file__).parent

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", 
        "http://127.0.0.1:3000", 
        "http://localhost:80",
        "http://127.0.0.1:80",
        "http://app.grither.exchange",
        "https://app.grither.exchange",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket менеджер для живых обновлений
class WebSocketManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.frontend_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, frontend_id: Optional[str] = None):
        await websocket.accept()
        self.active_connections.append(websocket)
        
        if frontend_id:
            if frontend_id not in self.frontend_connections:
                self.frontend_connections[frontend_id] = []
            self.frontend_connections[frontend_id].append(websocket)

    def disconnect(self, websocket: WebSocket, frontend_id: Optional[str] = None):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        
        if frontend_id and frontend_id in self.frontend_connections:
            if websocket in self.frontend_connections[frontend_id]:
                self.frontend_connections[frontend_id].remove(websocket)

    async def send_to_all(self, message: dict):
        """Отправить сообщение всем подключенным клиентам"""
        if not self.active_connections:
            return
            
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                disconnected.append(connection)
                
        # Удаляем отключенные соединения
        for conn in disconnected:
            if conn in self.active_connections:
                self.active_connections.remove(conn)

    async def send_to_frontend(self, frontend_id: str, message: dict):
        """Отправить сообщение конкретному фронтенду"""
        if frontend_id not in self.frontend_connections:
            return
            
        disconnected = []
        for connection in self.frontend_connections[frontend_id]:
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                disconnected.append(connection)
                
        # Удаляем отключенные соединения
        for conn in disconnected:
            if conn in self.frontend_connections[frontend_id]:
                self.frontend_connections[frontend_id].remove(conn)

websocket_manager = WebSocketManager()

api_router = APIRouter(prefix="/api")

class StatusCheck(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class StatusCheckCreate(BaseModel):
    client_name: str

class RubRates(BaseModel):
    buy: float
    sell: float

class RubPayload(BaseModel):
    card: RubRates
    cash: RubRates

class SettingsUpdate(BaseModel):
    globalMargin: Optional[float] = None  # Оставляем для обратной совместимости
    globalMarginBuy: Optional[float] = None   # Новая маржа для покупки
    globalMarginSell: Optional[float] = None  # Новая маржа для продажи
    perCcyMargin: Optional[Dict[str, Optional[float]]] = None
    visibility: Optional[Dict[str, bool]] = None
    sortOrder: Optional[List[str]] = None
    baseRates: Optional[Dict[str, float]] = None
    baseRatesUnit: Optional[str] = None
    rateSource: Optional[str] = None
    exchangeUrl: Optional[str] = None

# Функции для работы с базой данных
async def get_settings() -> dict:
    return await db.get_settings()

async def save_settings(patch: dict) -> dict:
    return await db.save_settings(patch)

async def get_latest_cache() -> Optional[dict]:
    return await db.get_latest_cache()

async def save_cache(doc: dict):
    await db.save_cache(doc)

async def insert_snapshot(base_rates: dict):
    await db.insert_snapshot(base_rates)

async def record_changes(prev_items: List[dict], new_items: List[dict], source: str = "auto"):
    await db.record_changes(prev_items, new_items, source)

async def build_history_for_items(items: List[dict], source: str = "exchangerate-api.com") -> List[dict]:
    """
    НОВАЯ ЛОГИКА: Получаем реалистичную историю курсов за 24 часа только с exchangerate-api.com
    """
    for it in items:
        code = it["code"]
        current_rate = it.get("base", 0)
        
        if current_rate <= 0:
            logger.warning(f"❌ Некорректный базовый курс для {code}: {current_rate}")
            it["history"] = []
            it["changePercent"] = 0.0
            continue
        
        # Получаем реалистичную историю на основе текущего курса
        realistic_history = await run_in_threadpool(get_real_24h_history, code, current_rate, source)
        
        if realistic_history and len(realistic_history) >= 2:
            # Используем реалистичную историю
            it["history"] = realistic_history[-24:]  # Последние 24 часа
            
            # Рассчитываем процент изменения за 24 часа
            first_rate = realistic_history[0]
            last_rate = realistic_history[-1]
            
            if first_rate > 0:
                change_percent = ((last_rate - first_rate) / first_rate) * 100.0
                it["changePercent"] = round(change_percent, 2)
                logger.debug(f"✅ {code}: изменение {change_percent:.2f}% за 24ч")
            else:
                it["changePercent"] = 0.0
        else:
            # Если нет данных - оставляем пустым
            it["history"] = []
            it["changePercent"] = 0.0
            
    return items

@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.get("/rates")
async def get_rates(force: bool = Query(default=False)):
    settings = await get_settings()
    cache = await get_latest_cache()
    need_refresh = True
    now = datetime.utcnow()
    original_source = settings.get("rateSource", "exchangerate-api.com")  # Инициализируем в начале
    
    if cache and cache.get("updatedAt"):
        updated_at = cache["updatedAt"]
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        # если кэш не в THB — принудительно обновляем
        if cache.get("unit") != "THB":
            need_refresh = True
            cache = None
        else:
            # Обновляем кэш каждый час для экономии API запросов
            need_refresh = (now - updated_at) > timedelta(hours=1)
    prev_items = cache.get("items") if cache else []

    # Параметр force позволяет получить самые свежие данные
    if force:
        need_refresh = True
        logger.info("Force refresh requested - bypassing cache")

    # Инициализируем use_items и updated_at
    use_items = prev_items or []
    updated_at = cache.get("updatedAt") if cache else now

    if need_refresh:
        source = original_source  # Используем выбранный источник
        logger.info(f"Fetching fresh rates from {source}")
        base_rates = await run_in_threadpool(fetch_base_rates_rub, source)
        
        if not base_rates:
            # если провайдер недоступен — используем кэш если есть
            if cache and cache.get("baseRates") and cache.get("unit") == "THB":
                base_rates = cache.get("baseRates")
                logger.info("Using cached rates as source failed")
            else:
                # возвращаем ошибку вместо пустых данных
                logger.error("Rate source failed and no cache available")
                return {"generatedAt": now.isoformat(), "rub": settings.get("rubRates", {}), "items": [], "error": "Источник курсов недоступен", "settings": {k: settings.get(k) for k in ["globalMargin", "globalMarginBuy", "globalMarginSell", "perCcyMargin", "visibility", "sortOrder", "baseRates", "rateSource", "exchangeUrl"]}, "denominations": {k: v for k, v in DENOMS.items()}}
        # оверрайд базовых курсов (THB) из настроек, если задан
        if base_rates:
            for k, v in (settings.get("baseRates") or {}).items():
                base_rates[k] = float(v)
            items, base_map = compute_buy_sell(base_rates, settings)
            await insert_snapshot(base_map)
            await record_changes(prev_items or [], items, source="auto")
            # Добавляем информацию об использованном источнике в кэш
            cache_doc = {
                "unit": "THB", 
                "updatedAt": now.isoformat(), 
                "baseRates": base_map, 
                "items": items,
                "actualSource": source,  # Фактически использованный источник
                "requestedSource": original_source  # Источник, который выбрал пользователь
            }
            await save_cache(cache_doc)
            use_items = items
            updated_at = now

    use_items = await build_history_for_items(use_items, original_source)

    # Подготавливаем информацию об источнике для ответа
    source_info = {}
    if cache and cache.get("actualSource") and cache.get("requestedSource"):
        if cache["actualSource"] != cache["requestedSource"]:
            source_info = {
                "fallbackUsed": True,
                "requestedSource": cache["requestedSource"],
                "actualSource": cache["actualSource"]
            }

    response = {
        "generatedAt": (updated_at.isoformat() if isinstance(updated_at, datetime) else updated_at),
        "rub": settings.get("rubRates", DEFAULT_SETTINGS["rubRates"]),
        "items": use_items,
        "settings": {k: settings.get(k) for k in ["globalMargin", "globalMarginBuy", "globalMarginSell", "perCcyMargin", "visibility", "sortOrder", "baseRates", "rateSource", "exchangeUrl"]},
        "denominations": {k: v for k, v in DENOMS.items()},
    }
    
    # Добавляем информацию об источнике, если есть fallback
    if source_info:
        response["sourceInfo"] = source_info
        
    return response

@api_router.get("/public/rates")
async def public_rates():
    data = await get_rates()
    payload = build_public_payload(data["rub"], data["items"])
    return payload

@api_router.get("/settings")
async def get_settings_api():
    return await get_settings()

@api_router.post("/settings")
async def update_settings_api(body: SettingsUpdate):
    patch = {k: v for k, v in body.dict(exclude_none=True).items()}
    s = await save_settings(patch)
    
    # Отправляем уведомление об изменении настроек
    await websocket_manager.send_to_all({
        "type": "settings_updated",
        "changes": patch,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    return s

@api_router.get("/settings-last-modified")
async def get_settings_last_modified():
    """Получить время последнего изменения настроек"""
    try:
        from database import db
        async with db.get_connection() as conn:
            cursor = await conn.execute("SELECT updated_at FROM settings WHERE id = 'singleton'")
            row = await cursor.fetchone()
            if row:
                return {"lastModified": row[0]}
            else:
                return {"lastModified": None}
    except Exception as e:
        logger.error(f"Ошибка получения времени изменения настроек: {e}")
        return {"lastModified": None}

@api_router.post("/rub")
async def update_rub(body: RubPayload):
    s = await get_settings()
    old = s.get("rubRates", {})
    await save_settings({"rubRates": body.dict()})
    
    def log(kind: str, old_v: Optional[float], new_v: float):
        return (
            datetime.utcnow().isoformat(),
            "RUB",
            kind,
            old_v,
            new_v,
            "manual"
        )
    
    ops = []
    for t in ["card", "cash"]:
        for k in ["buy", "sell"]:
          old_v = None
          try:
              old_v = float(old.get(t, {}).get(k))
          except Exception:
              pass
          new_v = float(getattr(body, t).dict()[k])
          if old_v is None or round(old_v, 2) != round(new_v, 2):
              ops.append(log(f"{t}-{k}", old_v, new_v))
    
    if ops:
        # Сохраняем изменения в базу данных
        from database import db
        async with db.get_connection() as conn:
            await conn.executemany("""
                INSERT INTO changes (timestamp, code, kind, old_value, new_value, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ops)
            await conn.commit()
    
    updated_settings = await get_settings()
    
    # Отправляем уведомление о изменении рублевых курсов
    await websocket_manager.send_to_all({
        "type": "rub_rates_updated",
        "rub_rates": body.dict(),
        "timestamp": datetime.utcnow().isoformat()
    })
    
    return updated_settings

class RubHistoryReset(BaseModel):
    rubType: str  # "card" или "cash"

@api_router.post("/rub/reset-history")
async def reset_rub_history(body: RubHistoryReset):
    """Сброс истории изменений для рублевых курсов"""
    try:
        rub_type = body.rubType.lower()
        if rub_type not in ["card", "cash"]:
            raise HTTPException(status_code=400, detail="rubType должен быть 'card' или 'cash'")
        
        # Удаляем все записи изменений для указанного типа рубля
        deleted_count = await db.delete_rub_changes(rub_type)
        
        logger.info(f"🗑️ Удалено {deleted_count} записей истории для RUB {rub_type}")
        
        return {
            "success": True,
            "deleted_count": deleted_count,
            "message": f"История для {rub_type} успешно сброшена"
        }
        
    except Exception as e:
        logger.error(f"❌ Ошибка сброса истории RUB {body.rubType}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/rate-sources")
async def get_rate_sources():
    """Получить доступные источники курсов валют"""
    return {"sources": RATE_SOURCES}

@api_router.get("/rub/auto-rate")
async def get_rub_auto_rate(source: str = Query("exchangerate-api.com")):
    """Получить автоматический курс рубля из выбранного источника"""
    try:
        from fx_service import _get_rates
        
        logger.info(f"🔍 Получение автоматического курса RUB из источника: {source}")
        
        # Получаем курс THB и RUB относительно USD из выбранного источника
        rates = _get_rates("USD", ["THB", "RUB"], source)
        thb_per_usd = rates.get("THB")
        rub_per_usd = rates.get("RUB")
        
        logger.info(f"📊 Получены курсы: THB/USD={thb_per_usd}, RUB/USD={rub_per_usd}")
        
        if thb_per_usd and rub_per_usd and thb_per_usd > 0 and rub_per_usd > 0:
            # Вычисляем THB за 1 RUB: 
            # 1 USD = thb_per_usd THB
            # 1 USD = rub_per_usd RUB
            # Значит: 1 RUB = (thb_per_usd / rub_per_usd) THB
            thb_per_rub = thb_per_usd / rub_per_usd
            
            logger.info(f"💱 Базовый курс: 1 RUB = {thb_per_rub:.4f} THB")
            
            # Применяем небольшую маржу для наличных (1% разница между покупкой и продажей)
            margin = 0.005  # 0.5% в каждую сторону
            cash_buy = thb_per_rub * (1 - margin)   # Покупка рублей (банк покупает рубли дешевле)
            cash_sell = thb_per_rub * (1 + margin)  # Продажа рублей (банк продает рубли дороже)
            
            logger.info(f"✅ Итоговые курсы: покупка={cash_buy:.4f}, продажа={cash_sell:.4f}")
            
            return {
                "success": True,
                "source": source,
                "thb_per_rub": round(thb_per_rub, 4),
                "cash_buy": round(cash_buy, 4),
                "cash_sell": round(cash_sell, 4),
                "margin_percent": margin * 100,
                "debug_info": {
                    "thb_per_usd": thb_per_usd,
                    "rub_per_usd": rub_per_usd
                }
            }
        else:
            error_msg = f"Не удалось получить курсы из источника {source}. THB/USD={thb_per_usd}, RUB/USD={rub_per_usd}"
            logger.error(f"❌ {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
            
    except Exception as e:
        logger.error(f"❌ Ошибка получения автоматического курса RUB: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/health")
async def health_check():
    """Проверка здоровья сервера"""
    try:
        # Проверяем подключение к базе данных
        await get_settings()
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "database": "connected",
            "version": "1.0.0"
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@api_router.get("/changes")
async def get_changes(limit: int = 100):
    changes = await db.get_changes(limit)
    return {"items": changes}

# Новые endpoints для глобальных настроек
class GlobalSettingsUpdate(BaseModel):
    tvMode: Optional[bool] = None
    lightTheme: Optional[bool] = None
    viewMode: Optional[str] = None  # 'table' или 'tiles'
    calculatorEnabled: Optional[bool] = None
    portraitMode: Optional[bool] = None
    frontendId: Optional[str] = None  # ID фронтенда для индивидуальных настроек

class FrontendSpecificSettings(BaseModel):
    """Настройки для конкретного фронтенда"""
    frontendId: str
    tvMode: Optional[bool] = None
    lightTheme: Optional[bool] = None
    viewMode: Optional[str] = None
    calculatorEnabled: Optional[bool] = None
    portraitMode: Optional[bool] = None

# Удален endpoint /settings-client - теперь это React роут

@api_router.get("/global-settings")
async def get_global_settings(request: Request, frontend: Optional[str] = Query(None)):
    """Получить глобальные настройки интерфейса для фронтенда"""
    # Логируем заголовки для отладки
    print(f"DEBUG: X-Frontend-Id header: {request.headers.get('X-Frontend-Id', 'NOT_SET')}")
    print(f"DEBUG: Query frontend: {frontend}")
    print(f"DEBUG: All headers: {dict(request.headers)}")
    
    # Определяем frontend_id из заголовка или query параметра
    frontend_id = None
    header_frontend_id = request.headers.get("X-Frontend-Id")
    
    if header_frontend_id and header_frontend_id != "default":
        frontend_id = header_frontend_id
        print(f"DEBUG: Using header frontend_id: {frontend_id}")
    elif frontend:
        frontend_id = frontend
        print(f"DEBUG: Using query frontend: {frontend_id}")
    else:
        frontend_id = "default"
        print(f"DEBUG: Using default frontend_id: {frontend_id}")
    
    settings = await get_settings()
    
    if frontend_id:
        # Получаем настройки для конкретного фронтенда
        frontend_settings = settings.get("frontendSettings", {}).get(frontend_id, {})
        return {
            "frontendId": frontend_id,
            "tvMode": frontend_settings.get("tvMode", settings.get("tvMode", False)),
            "lightTheme": frontend_settings.get("lightTheme", settings.get("lightTheme", False)),
            "viewMode": frontend_settings.get("viewMode", settings.get("viewMode", "table")),
            "calculatorEnabled": frontend_settings.get("calculatorEnabled", settings.get("calculatorEnabled", True)),
            "portraitMode": frontend_settings.get("portraitMode", settings.get("portraitMode", False))
        }
    else:
        # Возвращаем глобальные настройки
        return {
            "tvMode": settings.get("tvMode", False),
            "lightTheme": settings.get("lightTheme", False), 
            "viewMode": settings.get("viewMode", "table"),
            "calculatorEnabled": settings.get("calculatorEnabled", True),
            "portraitMode": settings.get("portraitMode", False)
        }

@api_router.post("/global-settings")
async def update_global_settings(body: GlobalSettingsUpdate):
    """Обновить глобальные настройки интерфейса"""
    patch = {k: v for k, v in body.dict(exclude_none=True).items()}
    frontend_id = patch.pop("frontendId", None)
    
    if frontend_id:
        # Обновляем настройки для конкретного фронтенда
        settings = await get_settings()
        frontend_settings = settings.get("frontendSettings", {})
        if frontend_id not in frontend_settings:
            frontend_settings[frontend_id] = {}
        
        # Обновляем настройки фронтенда
        frontend_settings[frontend_id].update(patch)
        await save_settings({"frontendSettings": frontend_settings})
        
        # Получаем обновленные настройки
        settings = await get_settings()
        frontend_settings = settings.get("frontendSettings", {}).get(frontend_id, {})
        updated_settings = {
            "frontendId": frontend_id,
            "tvMode": frontend_settings.get("tvMode", settings.get("tvMode", False)),
            "lightTheme": frontend_settings.get("lightTheme", settings.get("lightTheme", False)),
            "viewMode": frontend_settings.get("viewMode", settings.get("viewMode", "table")),
            "calculatorEnabled": frontend_settings.get("calculatorEnabled", settings.get("calculatorEnabled", True)),
            "portraitMode": frontend_settings.get("portraitMode", settings.get("portraitMode", False))
        }
        
        # Отправляем уведомление конкретному фронтенду
        await websocket_manager.send_to_frontend(frontend_id, {
            "type": "settings_updated",
            "frontend_id": frontend_id,
            "settings": updated_settings,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        return updated_settings
    else:
        # Обновляем глобальные настройки
        if patch:
            await save_settings(patch)
        
        settings = await get_settings()
        updated_settings = {
            "frontendId": "default", 
            "tvMode": settings.get("tvMode", False),
            "lightTheme": settings.get("lightTheme", False), 
            "viewMode": settings.get("viewMode", "table"),
            "calculatorEnabled": settings.get("calculatorEnabled", True),
            "portraitMode": settings.get("portraitMode", False)
        }
        
        # Отправляем уведомление всем клиентам о глобальных изменениях
        await websocket_manager.send_to_all({
            "type": "global_settings_updated",
            "settings": updated_settings,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        return updated_settings

@api_router.get("/frontends")
async def get_frontends():
    """Получить список доступных фронтендов"""
    settings = await get_settings()
    frontend_settings = settings.get("frontendSettings", {})
    
    frontends = [
        {"id": "main", "name": "Основной дисплей", "url": "/?frontend=main", "fullUrl": "http://app.grither.exchange/?frontend=main"},
        {"id": "tv", "name": "ТВ дисплей", "url": "/?frontend=tv", "fullUrl": "http://app.grither.exchange/?frontend=tv"},
        {"id": "mobile", "name": "Мобильные устройства", "url": "/?frontend=mobile", "fullUrl": "http://app.grither.exchange/?frontend=mobile"},
        {"id": "office", "name": "Офис", "url": "/?frontend=office", "fullUrl": "http://app.grither.exchange/?frontend=office"},
        {"id": "lobby", "name": "Лобби", "url": "/?frontend=lobby", "fullUrl": "http://app.grither.exchange/?frontend=lobby"},
        {"id": "exchange1", "name": "Обменник #1", "url": "/?frontend=exchange1", "fullUrl": "http://app.grither.exchange/?frontend=exchange1"},
        {"id": "exchange2", "name": "Обменник #2", "url": "/?frontend=exchange2", "fullUrl": "http://app.grither.exchange/?frontend=exchange2"},
        {"id": "exchange3", "name": "Обменник #3", "url": "/?frontend=exchange3", "fullUrl": "http://app.grither.exchange/?frontend=exchange3"}
    ]
    
    # Добавляем информацию о настройках для каждого фронтенда
    for frontend in frontends:
        frontend_id = frontend["id"]
        if frontend_id in frontend_settings:
            frontend["hasCustomSettings"] = True
            frontend["settings"] = frontend_settings[frontend_id]
        else:
            frontend["hasCustomSettings"] = False
            
    return {"frontends": frontends}

@api_router.get("/frontend-settings/{frontend_id}")
async def get_frontend_settings(frontend_id: str):
    """Получить настройки для конкретного фронтенда"""
    settings = await get_settings()
    frontend_settings = settings.get("frontendSettings", {})
    
    # Получаем базовые глобальные настройки
    base_settings = {
        "frontendId": frontend_id,
        "tvMode": settings.get("tvMode", False),
        "lightTheme": settings.get("lightTheme", False),
        "viewMode": settings.get("viewMode", "table"),
        "calculatorEnabled": settings.get("calculatorEnabled", True),
        "portraitMode": settings.get("portraitMode", False)
    }
    
    # Если есть индивидуальные настройки для этого фронтенда, применяем их
    if frontend_id in frontend_settings:
        base_settings.update(frontend_settings[frontend_id])
        base_settings["frontendId"] = frontend_id  # Сохраняем frontend_id
    
    return base_settings

@api_router.post("/frontend-settings")
async def update_frontend_settings(body: FrontendSpecificSettings):
    """Обновить настройки для конкретного фронтенда"""
    frontend_id = body.frontendId
    settings_dict = body.dict(exclude={"frontendId"}, exclude_none=True)
    
    settings = await get_settings()
    frontend_settings = settings.get("frontendSettings", {})
    
    if frontend_id not in frontend_settings:
        frontend_settings[frontend_id] = {}
    
    frontend_settings[frontend_id].update(settings_dict)
    await save_settings({"frontendSettings": frontend_settings})
    
    # Получаем обновленные настройки для фронтенда
    settings = await get_settings()
    base_settings = {
        "frontendId": frontend_id,
        "tvMode": settings.get("tvMode", False),
        "lightTheme": settings.get("lightTheme", False),
        "viewMode": settings.get("viewMode", "table"),
        "calculatorEnabled": settings.get("calculatorEnabled", True),
        "portraitMode": settings.get("portraitMode", False)
    }
    
    # Применяем индивидуальные настройки
    if frontend_id in frontend_settings:
        base_settings.update(frontend_settings[frontend_id])
        base_settings["frontendId"] = frontend_id
    
    updated_settings = base_settings
    
    # Отправляем уведомление конкретному фронтенду
    await websocket_manager.send_to_frontend(frontend_id, {
        "type": "settings_updated",
        "frontend_id": frontend_id,
        "settings": updated_settings,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    return updated_settings

@api_router.delete("/frontend-settings/{frontend_id}")
async def delete_frontend_settings(frontend_id: str):
    """Удалить персональные настройки фронтенда (вернуться к глобальным)"""
    settings = await get_settings()
    frontend_settings = settings.get("frontendSettings", {})
    
    if frontend_id in frontend_settings:
        del frontend_settings[frontend_id]
        await save_settings({"frontendSettings": frontend_settings})
        
        # Отправляем уведомление о сбросе настроек
        await websocket_manager.send_to_frontend(frontend_id, {
            "type": "settings_reset",
            "frontend_id": frontend_id,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    return {"success": True, "message": f"Настройки для {frontend_id} сброшены к глобальным"}

@api_router.post("/sync-rates")
async def sync_rates_to_clients():
    """Принудительная синхронизация курсов для всех клиентов"""
    # Получаем свежие данные
    rates_data = await get_rates(force=True)
    
    # Отправляем уведомление через WebSocket всем клиентам
    await websocket_manager.send_to_all({
        "type": "rates_updated",
        "data": rates_data,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    return {
        "success": True,
        "message": "Курсы синхронизированы",
        "data": rates_data
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, frontend_id: Optional[str] = None):
    """WebSocket endpoint для живых обновлений"""
    await websocket_manager.connect(websocket, frontend_id)
    
    try:
        # Отправляем начальное сообщение о подключении
        await websocket.send_text(json.dumps({
            "type": "connected",
            "frontend_id": frontend_id,
            "timestamp": datetime.utcnow().isoformat()
        }))
        
        while True:
            # Слушаем сообщения от клиента (пинг для поддержания соединения)
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get("type") == "ping":
                await websocket.send_text(json.dumps({
                    "type": "pong",
                    "timestamp": datetime.utcnow().isoformat()
                }))
                
    except WebSocketDisconnect:
        websocket_manager.disconnect(websocket, frontend_id)

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_obj = StatusCheck(client_name=input.client_name)
    await db.insert_status_check(status_obj.dict())
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks():
    status_checks = await db.get_status_checks()
    return [StatusCheck(**status_check) for status_check in status_checks]

app.include_router(api_router)
app.include_router(history_router, prefix="/api")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@app.on_event("startup")
async def startup_db():
    """Инициализация базы данных при запуске"""
    await db.init_database()
    logger.info("✅ SQLite база данных инициализирована")

@app.on_event("shutdown")
async def shutdown_db():
    """Закрытие соединений с базой данных"""
    logger.info("💤 Закрытие соединений с базой данных")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)