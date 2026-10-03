"""
Zonex X Bot — Single File Edition
==================================
Telegram bot with channel verification, referral, TG/Panel selling,
UPI payment requests, admin approval, and automatic delivery.

Requirements:
    pip install aiogram aiosqlite "SQLAlchemy[asyncio]" asyncpg python-dotenv "qrcode[pil]" Pillow

Run:
    python main.py
"""
from __future__ import annotations

import asyncio
from html import escape
import io
import logging
import os
import random
import string
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import qrcode
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.deep_linking import create_start_link
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("jonex")

# ============================================================
# CONFIG
# ============================================================
load_dotenv()


def _int_list(raw: str) -> List[int]:
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]


def _normalise_database_url(raw: str) -> str:
    """Make Railway's standard Postgres URL usable by SQLAlchemy's async engine."""
    raw = raw.strip()
    if not raw:
        raise RuntimeError("DATABASE_URL is empty")

    parsed = urlsplit(raw)
    asyncpg_schemes = {
        "postgres",
        "postgresql",
        "postgresql+psycopg",
        "postgresql+psycopg2",
    }
    if parsed.scheme not in asyncpg_schemes | {"postgresql+asyncpg"}:
        # Preserve non-Postgres URLs exactly (notably SQLite's four-slash paths).
        return raw

    scheme = "postgresql+asyncpg" if parsed.scheme in asyncpg_schemes else parsed.scheme

    # Railway and other providers may append libpq's sslmode. asyncpg uses `ssl`.
    query = parse_qsl(parsed.query, keep_blank_values=True)
    sslmode = next((value for key, value in query if key == "sslmode"), None)
    if sslmode is not None and not any(key == "ssl" for key, _ in query):
        query = [(key, value) for key, value in query if key != "sslmode"]
        query.append(("ssl", sslmode))

    return urlunsplit(
        (scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


@dataclass(frozen=True)
class Config:
    bot_token: str = "".join(
        os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'").split()
    )
    admin_ids: List[int] = field(default_factory=lambda: _int_list(os.getenv("ADMIN_IDS", "")))
    database_url: str = _normalise_database_url(
        os.getenv("DATABASE_URL", "sqlite+aiosqlite:///jonex.db")
    )

    # Public usernames are safer defaults than hard-coded numeric IDs. The
    # link is also used as a fallback when a custom numeric ID is configured.
    channel_1_id: str = os.getenv("CHANNEL_1_ID", "@SMARTEARNERS011")
    channel_2_id: str = os.getenv("CHANNEL_2_ID", "@zonexarmy")
    channel_1_name: str = os.getenv("CHANNEL_1_NAME", "💰 SMART EARNERS 💰")
    channel_2_name: str = os.getenv("CHANNEL_2_NAME", "ZoneXarmy")
    channel_1_link: str = os.getenv("CHANNEL_1_LINK", "https://t.me/SMARTEARNERS011")
    channel_2_link: str = os.getenv("CHANNEL_2_LINK", "https://t.me/zonexarmy")

    default_upi_id: str = os.getenv("PAYMENT_UPI_ID", "")
    default_currency: str = os.getenv("PAYMENT_CURRENCY", "₹")


config = Config()
MANUAL_DELIVERY_SENTINEL = "__MANUAL_DELIVERY__"

if not config.bot_token:
    raise RuntimeError("BOT_TOKEN is not set in .env")
if not config.admin_ids:
    raise RuntimeError("ADMIN_IDS is not set in .env")

# ============================================================
# DATABASE MODELS
# ============================================================
class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    referral_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    referred_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    total_referrals: Mapped[int] = mapped_column(Integer, default=0)
    successful_referrals: Mapped[int] = mapped_column(Integer, default=0)
    tg_purchases: Mapped[int] = mapped_column(Integer, default=0)
    panel_purchases: Mapped[int] = mapped_column(Integer, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    banned_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    banned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class TgPackage(Base):
    __tablename__ = "tg_packages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Panel(Base):
    __tablename__ = "panels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Float)
    delivery_info: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    credentials: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    referral_requirement: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    product_type: Mapped[str] = mapped_column(String(16))
    product_id: Mapped[int] = mapped_column(Integer)
    product_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    payment_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    delivery_data: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class PaymentRequest(Base):
    __tablename__ = "payment_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(32), ForeignKey("orders.order_id"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount: Mapped[float] = mapped_column(Float)
    proof: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    reviewed_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ReferralRecord(Base):
    __tablename__ = "referrals"
    __table_args__ = (UniqueConstraint("referrer_id", "referred_id", name="uq_referral"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    referred_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BotSetting(Base):
    __tablename__ = "bot_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# ============================================================
# DATABASE ENGINE
# ============================================================
engine = create_async_engine(
    config.database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args={"timeout": 30} if config.database_url.startswith("sqlite") else {},
)
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # seed default settings
    async with SessionFactory() as s:
        defaults = {
            "referral_tg_requirement": "15",
            "referral_panel_requirement": "10",
            "payment_upi_id": config.default_upi_id,
            "payment_instructions": "Pay to the UPI ID above and submit your transaction reference.",
            "payment_currency": config.default_currency,
            "maintenance_global": "false",
            "maintenance_tg": "false",
            "maintenance_panel": "false",
            "tg_delivery_note": "✅ Your TG order is approved. Admin will contact you shortly.",
        }
        for k, v in defaults.items():
            row = await s.get(BotSetting, k)
            if not row:
                s.add(BotSetting(key=k, value=v))
            elif k == "referral_panel_requirement" and row.value == "4":
                # Migrate the original default to the requested 10 referrals.
                row.value = v

        panel_count = (await s.execute(select(func.count(Panel.id)))).scalar() or 0
        if panel_count == 0:
            s.add_all(
                [
                    Panel(
                        name="Panel — 5 Hours",
                        description="Panel access for 5 hours",
                        price=59,
                        delivery_info="Admin will deliver the panel manually after payment approval.",
                        credentials=MANUAL_DELIVERY_SENTINEL,
                        stock=1,
                    ),
                    Panel(
                        name="Panel — 7 Hours",
                        description="Panel access for 7 hours",
                        price=99,
                        delivery_info="Admin will deliver the panel manually after payment approval.",
                        credentials=MANUAL_DELIVERY_SENTINEL,
                        stock=1,
                    ),
                    Panel(
                        name="Panel — 24 Hours",
                        description="Panel access for 24 hours",
                        price=199,
                        delivery_info="Admin will deliver the panel manually after payment approval.",
                        credentials=MANUAL_DELIVERY_SENTINEL,
                        stock=1,
                    ),
                ]
            )

        existing_tg_names = set(
            (await s.execute(select(TgPackage.name))).scalars().all()
        )
        tg_defaults = [
            TgPackage(name="1 TG", quantity=1, price=35),
            TgPackage(name="2 TG", quantity=2, price=60),
            TgPackage(name="5 TG", quantity=5, price=140),
        ]
        for package in tg_defaults:
            if package.name not in existing_tg_names:
                s.add(package)
        await s.commit()
    logger.info("Database initialised (backend=%s)", engine.url.get_backend_name())


@asynccontextmanager
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionFactory() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


# ============================================================
# DATABASE QUERIES
# ============================================================
def gen_order_id() -> str:
    return f"JNX-{_utcnow().strftime('%Y%m%d')}-{''.join(random.choices(string.digits, k=8))}"


def gen_ref_code(uid: int) -> str:
    return f"ref_{uid}"


async def get_or_create_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    referrer_id: Optional[int] = None,
) -> User:
    async with db_session() as s:
        res = await s.execute(select(User).where(User.telegram_id == telegram_id))
        user = res.scalar_one_or_none()
        if user:
            user.username = username or user.username
            user.first_name = first_name or user.first_name
            user.last_name = last_name or user.last_name
            return user

        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            referral_code=gen_ref_code(telegram_id),
        )
        s.add(user)
        try:
            await s.flush()
        except IntegrityError:
            await s.rollback()
            res = await s.execute(select(User).where(User.telegram_id == telegram_id))
            return res.scalar_one()

        if referrer_id and referrer_id != telegram_id:
            res = await s.execute(select(User).where(User.telegram_id == referrer_id))
            ref_user = res.scalar_one_or_none()
            if ref_user:
                user.referred_by = referrer_id
                ref_user.total_referrals += 1
                s.add(ReferralRecord(referrer_id=referrer_id, referred_id=telegram_id))
        return user


async def get_user(tid: int) -> Optional[User]:
    async with db_session() as s:
        res = await s.execute(select(User).where(User.telegram_id == tid))
        return res.scalar_one_or_none()


async def get_user_by_username(uname: str) -> Optional[User]:
    uname = uname.lstrip("@")
    async with db_session() as s:
        res = await s.execute(select(User).where(User.username == uname))
        return res.scalar_one_or_none()


async def mark_verified(tid: int) -> Optional[int]:
    """Mark a user verified once; return their referrer ID only for first verification."""
    async with db_session() as s:
        row = (
            await s.execute(
                select(User.referred_by)
                .where(User.telegram_id == tid, User.verified.is_(False))
                .with_for_update()
            )
        ).first()
        if row is None:
            return None
        await s.execute(
            update(User)
            .where(User.telegram_id == tid, User.verified.is_(False))
            .values(verified=True)
        )
        return int(row[0]) if row[0] is not None else 0


async def set_banned(tid: int, banned: bool, reason: str = "", admin_id: int = 0) -> None:
    async with db_session() as s:
        vals: Dict[str, Any] = {"is_banned": banned}
        if banned:
            vals.update(ban_reason=reason, banned_by=admin_id, banned_at=_utcnow())
        else:
            vals.update(ban_reason=None, banned_by=None, banned_at=None)
        await s.execute(update(User).where(User.telegram_id == tid).values(**vals))


async def increment_successful_ref(tid: int) -> None:
    async with db_session() as s:
        await s.execute(
            update(User)
            .where(User.telegram_id == tid)
            .values(successful_referrals=User.successful_referrals + 1)
        )


async def increment_purchase(tid: int, ptype: str) -> None:
    async with db_session() as s:
        col = User.tg_purchases if ptype.upper() == "TG" else User.panel_purchases
        await s.execute(
            update(User).where(User.telegram_id == tid).values({col.key: col + 1})
        )


async def get_setting(key: str, default: str = "") -> str:
    async with db_session() as s:
        row = await s.get(BotSetting, key)
        return row.value if row else default


async def set_setting(key: str, value: str) -> None:
    async with db_session() as s:
        row = await s.get(BotSetting, key)
        if row:
            row.value = value
        else:
            s.add(BotSetting(key=key, value=value))


async def list_tg_packages(active_only: bool = True) -> List[TgPackage]:
    async with db_session() as s:
        stmt = select(TgPackage).order_by(TgPackage.id)
        if active_only:
            stmt = stmt.where(TgPackage.is_active.is_(True))
        return list((await s.execute(stmt)).scalars().all())


async def get_tg_package(pid: int) -> Optional[TgPackage]:
    async with db_session() as s:
        return await s.get(TgPackage, pid)


async def list_panels(active_only: bool = True) -> List[Panel]:
    async with db_session() as s:
        stmt = select(Panel).order_by(Panel.id)
        if active_only:
            stmt = stmt.where(Panel.is_active.is_(True))
        return list((await s.execute(stmt)).scalars().all())


async def get_panel(pid: int) -> Optional[Panel]:
    async with db_session() as s:
        return await s.get(Panel, pid)


async def decrement_panel_stock(pid: int) -> bool:
    """Atomically reserve one in-stock panel so concurrent approvals cannot oversell."""
    async with db_session() as s:
        result = await s.execute(
            update(Panel)
            .where(Panel.id == pid, Panel.stock > 0)
            .values(stock=Panel.stock - 1)
        )
        return result.rowcount == 1


async def create_order(
    order_id: str, user_id: int, ptype: str, pid: int, pname: str, qty: int, amt: float
) -> Order:
    async with db_session() as s:
        o = Order(
            order_id=order_id,
            user_id=user_id,
            product_type=ptype.upper(),
            product_id=pid,
            product_name=pname,
            quantity=qty,
            amount=amt,
            status="PENDING",
        )
        s.add(o)
        return o


async def get_order(order_id: str) -> Optional[Order]:
    async with db_session() as s:
        res = await s.execute(select(Order).where(Order.order_id == order_id))
        return res.scalar_one_or_none()


async def update_order(order_id: str, status: str, **kw: Any) -> bool:
    async with db_session() as s:
        res = await s.execute(select(Order).where(Order.order_id == order_id))
        o = res.scalar_one_or_none()
        if not o:
            return False
        o.status = status
        for k, v in kw.items():
            if hasattr(o, k):
                setattr(o, k, v)
        return True


async def transition_order(
    order_id: str, expected_statuses: tuple[str, ...], new_status: str
) -> bool:
    """Atomically claim an order transition so duplicate admin clicks are harmless."""
    async with db_session() as s:
        result = await s.execute(
            update(Order)
            .where(Order.order_id == order_id, Order.status.in_(expected_statuses))
            .values(status=new_status)
        )
        return result.rowcount == 1


async def create_payment_request(order_id: str, uid: int, amt: float, proof: str) -> None:
    async with db_session() as s:
        s.add(PaymentRequest(order_id=order_id, user_id=uid, amount=amt, proof=proof))


async def submit_payment_proof(order_id: str, uid: int, amount: float, proof: str) -> bool:
    """Save one proof and advance only its owner's still-pending order."""
    async with db_session() as s:
        result = await s.execute(
            update(Order)
            .where(
                Order.order_id == order_id,
                Order.user_id == uid,
                Order.status == "PENDING",
            )
            .values(status="PAYMENT_SUBMITTED", payment_ref=proof)
        )
        if result.rowcount != 1:
            return False
        s.add(PaymentRequest(order_id=order_id, user_id=uid, amount=amount, proof=proof))
        return True


async def update_payment_status(order_id: str, status: str, admin_id: int) -> None:
    async with db_session() as s:
        res = await s.execute(
            select(PaymentRequest)
            .where(PaymentRequest.order_id == order_id)
            .order_by(PaymentRequest.id.desc())
        )
        pr = res.scalars().first()
        if pr:
            pr.status = status
            pr.reviewed_by = admin_id
            pr.reviewed_at = _utcnow()


async def get_stats() -> Dict[str, Any]:
    async with db_session() as s:
        total = (await s.execute(select(func.count(User.id)))).scalar() or 0
        verified = (
            await s.execute(select(func.count(User.id)).where(User.verified.is_(True)))
        ).scalar() or 0
        banned = (
            await s.execute(select(func.count(User.id)).where(User.is_banned.is_(True)))
        ).scalar() or 0
        orders = (await s.execute(select(func.count(Order.id)))).scalar() or 0
        pending = (
            await s.execute(select(func.count(Order.id)).where(Order.status == "PENDING"))
        ).scalar() or 0
        approved = (
            await s.execute(
                select(func.count(Order.id)).where(
                    Order.status.in_(["APPROVED", "DELIVERED"])
                )
            )
        ).scalar() or 0
        revenue = (
            await s.execute(
                select(func.sum(Order.amount)).where(
                    Order.status.in_(["APPROVED", "DELIVERED"])
                )
            )
        ).scalar() or 0.0
        return {
            "total_users": total,
            "verified": verified,
            "banned": banned,
            "orders": orders,
            "pending": pending,
            "approved": approved,
            "revenue": float(revenue),
        }


async def all_user_ids() -> List[int]:
    async with db_session() as s:
        res = await s.execute(select(User.telegram_id).where(User.is_banned.is_(False)))
        return [r[0] for r in res.all()]


# ============================================================
# HELPERS / SERVICES
# ============================================================
def build_upi_link(upi_id: str, name: str, amount: float, note: str = "") -> str:
    from urllib.parse import quote
    link = f"upi://pay?pa={quote(upi_id)}&pn={quote(name)}&am={amount:.2f}&cu=INR"
    if note:
        link += f"&tn={quote(note)}"
    return link


def generate_qr(data: str) -> BufferedInputFile:
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return BufferedInputFile(buf.read(), filename="upi_qr.png")


async def get_payment_details() -> Dict[str, str]:
    return {
        "upi_id": await get_setting("payment_upi_id", config.default_upi_id),
        "instructions": await get_setting("payment_instructions", "Pay and submit reference."),
        "currency": await get_setting("payment_currency", config.default_currency),
        "qr_file_id": await get_setting("payment_qr_file_id", ""),
    }


def is_admin(uid: int) -> bool:
    return uid in config.admin_ids


def display_name(u: types.User) -> str:
    if u.full_name:
        return u.full_name
    if u.username:
        return f"@{u.username}"
    return str(u.id)


async def can_claim_tg_reward(uid: int) -> bool:
    user = await get_user(uid)
    if not user:
        return False
    try:
        req = int(await get_setting("referral_tg_requirement", "15"))
        claimed = int(await get_setting(f"tg_claimed_{uid}", "0"))
    except (TypeError, ValueError):
        return False
    return req > 0 and claimed < (user.successful_referrals // req)


async def can_claim_panel_reward(uid: int) -> bool:
    user = await get_user(uid)
    if not user:
        return False
    try:
        req = int(await get_setting("referral_panel_requirement", "10"))
        claimed = int(await get_setting(f"panel_claimed_{uid}", "0"))
    except (TypeError, ValueError):
        return False
    return req > 0 and claimed < (user.successful_referrals // req)


async def record_reward(uid: int, kind: str) -> bool:
    """Record a referral reward only if a new milestone is still unclaimed."""
    if kind not in ("tg", "panel"):
        return False
    async with db_session() as s:
        user = (
            await s.execute(
                select(User).where(User.telegram_id == uid).with_for_update()
            )
        ).scalar_one_or_none()
        if not user:
            return False
        requirement_key = "referral_tg_requirement" if kind == "tg" else "referral_panel_requirement"
        requirement_row = await s.get(BotSetting, requirement_key)
        try:
            requirement = int(requirement_row.value) if requirement_row else (15 if kind == "tg" else 10)
        except (TypeError, ValueError):
            return False
        if requirement < 1:
            return False
        key = f"{kind}_claimed_{uid}"
        claimed_row = await s.get(BotSetting, key, with_for_update=True)
        try:
            claimed = int(claimed_row.value) if claimed_row else 0
        except (TypeError, ValueError):
            claimed = 0
        if claimed >= user.successful_referrals // requirement:
            return False
        if claimed_row:
            claimed_row.value = str(claimed + 1)
        else:
            s.add(BotSetting(key=key, value="1"))
        return True


_channel_access_alerted = False


def _channel_candidates(chat_id: str, channel_link: str) -> List[str]:
    candidates: List[str] = []
    for value in (chat_id, channel_link):
        if not value:
            continue
        candidate = value.strip()
        if candidate.startswith("https://t.me/") or candidate.startswith("http://t.me/"):
            candidate = candidate.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
            if candidate.startswith("+") or candidate.lower().startswith("joinchat/"):
                continue
            candidate = f"@{candidate.lstrip('@')}"
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


async def is_channel_member(
    bot: Bot, chat_id: str, uid: int, channel_link: str = ""
) -> Optional[bool]:
    candidates = _channel_candidates(chat_id, channel_link)
    if not candidates:
        return True

    for candidate in candidates:
        try:
            m = await bot.get_chat_member(chat_id=candidate, user_id=uid)
            return m.status not in ("left", "kicked")
        except TelegramAPIError as e:
            logger.warning("Membership check unavailable for %s: %s", candidate, e)
        except Exception as e:
            logger.exception("Unexpected membership-check error for %s: %s", candidate, e)
            continue
    # An API error does not prove that the user has not joined. Keep the
    # requirement fail-closed, but let the handler show an accurate error.
    return None


async def send_to_admins(bot: Bot, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    for aid in config.admin_ids:
        try:
            await bot.send_message(aid, text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logger.error("Failed to notify admin %s: %s", aid, e)


async def send_payment_review(
    bot: Bot, text: str, order_id: str, proof_text: str = "", proof_photo_id: str = ""
) -> None:
    """Send approval controls plus the actual payment proof to every configured admin."""
    for aid in config.admin_ids:
        try:
            if proof_photo_id:
                await bot.send_photo(
                    aid,
                    proof_photo_id,
                    caption=text,
                    reply_markup=approval_kb(order_id),
                    parse_mode="HTML",
                )
            else:
                safe_proof = escape(proof_text.strip())[:1200] or "Not provided"
                await bot.send_message(
                    aid,
                    f"{text}\n<b>Payment reference:</b>\n<code>{safe_proof}</code>",
                    reply_markup=approval_kb(order_id),
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error("Failed to send payment review for %s to admin %s: %s", order_id, aid, e)


async def notify_referral_success(bot: Bot, referrer: User, referred: User) -> None:
    tg_req = int(await get_setting("referral_tg_requirement", "15"))
    panel_req = int(await get_setting("referral_panel_requirement", "10"))
    referrer_name = escape(
        " ".join(filter(None, [referrer.first_name, referrer.last_name]))
        or str(referrer.telegram_id)
    )
    referred_name = escape(
        " ".join(filter(None, [referred.first_name, referred.last_name]))
        or str(referred.telegram_id)
    )
    referrer_username = f"@{escape(referrer.username)}" if referrer.username else "—"
    referred_username = f"@{escape(referred.username)}" if referred.username else "—"
    count = referrer.successful_referrals
    milestones: List[str] = []
    if tg_req > 0 and count % tg_req == 0:
        milestones.append(f"🎉 TG reward ka {count}/{tg_req} target complete ho gaya.")
    if panel_req > 0 and count % panel_req == 0:
        milestones.append(f"🎉 Panel reward ka {count}/{panel_req} target complete ho gaya.")

    text = (
        "🔔 <b>Successful Referral</b>\n\n"
        f"<b>Referrer:</b> {referrer_name} ({referrer_username})\n"
        f"Referrer ID: <code>{referrer.telegram_id}</code>\n\n"
        f"<b>New Member:</b> {referred_name} ({referred_username})\n"
        f"New Member ID: <code>{referred.telegram_id}</code>\n\n"
        f"👥 Successful Referrals: <b>{count}</b>\n"
        f"📱 TG Progress: {count}/{tg_req}\n"
        f"🛒 Panel Progress: {count}/{panel_req}"
    )
    if milestones:
        text += "\n\n" + "\n".join(milestones)
    await send_to_admins(bot, text)

    if milestones:
        try:
            await bot.send_message(
                referrer.telegram_id,
                "🎉 <b>Referral Target Complete!</b>\n\n"
                + "\n".join(milestones)
                + f"\n\nTotal successful referrals: {count}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error("Referral milestone notification failed: %s", e)


# ============================================================
# KEYBOARDS
# ============================================================
def verify_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if config.channel_1_id:
        kb.button(text=f"📢 {config.channel_1_name}", url=config.channel_1_link)
    if config.channel_2_id:
        kb.button(text=f"📢 {config.channel_2_name}", url=config.channel_2_link)
    kb.button(text="✅ I Have Verified", callback_data="verify_check")
    kb.adjust(1)
    return kb.as_markup()


def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Panel", callback_data="user_panel")
    kb.button(text="📱 TG", callback_data="user_tg")
    kb.button(text="🎁 Referral", callback_data="user_referral")
    kb.adjust(2, 1)
    return kb.as_markup()


def tg_menu_kb(requirement: int = 15) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🎁 Refer {requirement} → 1 TG FREE", callback_data="tg_referral")
    kb.button(text="💳 Buy TG", callback_data="tg_buy")
    kb.button(text="🔙 Back", callback_data="user_back")
    kb.adjust(1)
    return kb.as_markup()


def panel_options_kb(requirement: int = 10) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"🎁 Refer {requirement} → 1 Panel FREE", callback_data="panel_referral")
    kb.button(text="💳 Buy Panel", callback_data="panel_buy")
    kb.button(text="🔙 Back", callback_data="user_back")
    kb.adjust(1)
    return kb.as_markup()


def panel_menu_kb(panels: List[Panel]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in panels:
        kb.button(text=f"{p.name} — ₹{p.price:.0f}", callback_data=f"panel_select_{p.id}")
    kb.button(text="🔙 Back", callback_data="user_back")
    kb.adjust(1)
    return kb.as_markup()


def parse_tg_package_input(raw: str) -> tuple[str, int, float]:
    separator = "|" if "|" in raw else "/"
    parts = [x.strip() for x in raw.split(separator)]
    if len(parts) != 3:
        raise ValueError("Use Name/Quantity/Price")
    name, qty, price = parts
    if not name:
        raise ValueError("Package name is required")
    return name, int(qty), float(price)


def cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel", callback_data="cancel_action")
    return kb.as_markup()


def admin_main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Users", callback_data="admin_users")
    kb.button(text="📊 Statistics", callback_data="admin_stats")
    kb.button(text="📢 Broadcast", callback_data="admin_broadcast")
    kb.button(text="🛒 Manage Panels", callback_data="admin_panels")
    kb.button(text="📱 Manage TG", callback_data="admin_tg")
    kb.button(text="💰 Payments", callback_data="admin_payments")
    kb.button(text="🎁 Referral Settings", callback_data="admin_referral")
    kb.button(text="💳 Payment Settings", callback_data="admin_payment_settings")
    kb.button(text="🔗 Channel Settings", callback_data="admin_channels")
    kb.button(text="🟢 Bot Status", callback_data="admin_status")
    kb.button(text="🛠 Maintenance", callback_data="admin_maintenance")
    kb.button(text="🚫 Ban/Unban", callback_data="admin_ban_menu")
    kb.adjust(2, 2, 2, 2, 2, 2)
    return kb.as_markup()


def approval_kb(order_id: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve", callback_data=f"approve_{order_id}")
    kb.button(text="❌ Reject", callback_data=f"reject_{order_id}")
    kb.adjust(2)
    return kb.as_markup()


def back_admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Admin Panel", callback_data="admin_back")
    return kb.as_markup()


# ============================================================
# FSM STATES
# ============================================================
class TgStates(StatesGroup):
    waiting_proof = State()


class PanelStates(StatesGroup):
    waiting_proof = State()


class AdminStates(StatesGroup):
    broadcast = State()
    find_user = State()
    ban_user = State()
    unban_user = State()
    add_panel = State()
    add_tg = State()
    set_tg_req = State()
    set_panel_req = State()
    set_upi = State()
    set_qr = State()


# ============================================================
# ROUTERS
# ============================================================
router = Router(name="main")

WELCOME_MAIN = (
    "<b>Zonex X Bot</b>\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "👋 Welcome, <b>{name}</b>!\n\n"
    "✅ Verification Complete\n\n"
    "You can now use the bot.\n\n"
    "Choose an option below:"
)

WELCOME_VERIFY = (
    "<b>Zonex X Bot</b>\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "👋 Welcome, <b>{name}</b>!\n\n"
    "Welcome to Zonex X Bot.\n\n"
    "Join both required channels. The bot will verify you automatically after you join them. "
    "If it does not update, tap “I Have Verified” below.\n\n"
    "<b>Required Channels:</b>\n"
    "{channels}"
)


# ------------------------- START -------------------------
async def _send_welcome(msg: Message, verified: bool) -> None:
    name = escape(display_name(msg.from_user))
    if verified:
        await msg.answer(WELCOME_MAIN.format(name=name), reply_markup=main_menu_kb(), parse_mode="HTML")
    else:
        channels = []
        if config.channel_1_id:
            channels.append(f"🔹 {config.channel_1_name}")
        if config.channel_2_id:
            channels.append(f"🔹 {config.channel_2_name}")
        channel_text = "\n".join(channels) or "🔹 No channel configured"
        await msg.answer(
            WELCOME_VERIFY.format(name=name, channels=channel_text),
            reply_markup=verify_kb(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


@router.message(CommandStart(deep_link=True))
async def start_deep(msg: Message, command: CommandObject) -> None:
    ref_id: Optional[int] = None
    if command.args and command.args.startswith("ref_"):
        try:
            ref_id = int(command.args.replace("ref_", ""))
        except ValueError:
            ref_id = None

    user = await get_or_create_user(
        telegram_id=msg.from_user.id,
        username=msg.from_user.username,
        first_name=msg.from_user.first_name,
        last_name=msg.from_user.last_name,
        referrer_id=ref_id,
    )
    if user.is_banned:
        await msg.answer("🚫 You are banned from using this bot.")
        return
    await _send_welcome(msg, user.verified)


@router.message(CommandStart())
async def start_plain(msg: Message) -> None:
    user = await get_or_create_user(
        telegram_id=msg.from_user.id,
        username=msg.from_user.username,
        first_name=msg.from_user.first_name,
        last_name=msg.from_user.last_name,
    )
    if user.is_banned:
        await msg.answer("🚫 You are banned from using this bot.")
        return
    await _send_welcome(msg, user.verified)


# ------------------------- VERIFY -------------------------
@router.callback_query(F.data == "verify_check")
async def cb_verify(call: CallbackQuery) -> None:
    global _channel_access_alerted
    uid = call.from_user.id
    bot = call.bot

    ch1_ok = await is_channel_member(bot, config.channel_1_id, uid, config.channel_1_link)
    ch2_ok = await is_channel_member(bot, config.channel_2_id, uid, config.channel_2_link)

    if ch1_ok is None or ch2_ok is None:
        await call.answer(
            "⚠️ I can't check channel membership right now. Please contact the bot admin.",
            show_alert=True,
        )
        if not _channel_access_alerted:
            _channel_access_alerted = True
            await send_to_admins(
                bot,
                "⚠️ <b>Channel verification is blocked by Telegram</b>\n\n"
                "The bot cannot check members in one or both configured channels. "
                "Add @ZonexXearningBot as an administrator in both channels, "
                "then users can press “I Have Verified” again.\n\n"
                f"Channel 1: <code>{escape(config.channel_1_id)}</code>\n"
                f"Channel 2: <code>{escape(config.channel_2_id)}</code>",
            )
        return

    if not (ch1_ok and ch2_ok):
        await call.answer("❌ Please join both channels first.", show_alert=True)
        return

    referrer_id = await mark_verified(uid)
    if referrer_id:
        await increment_successful_ref(referrer_id)
        referrer = await get_user(referrer_id)
        verified_user = await get_user(uid)
        if referrer and verified_user:
            await notify_referral_success(bot, referrer, verified_user)

    await call.message.edit_text(
        WELCOME_MAIN.format(name=escape(call.from_user.full_name)),
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await call.answer("✅ Verified!")


def _is_member_status(member: Any) -> bool:
    if member.status in ("creator", "administrator", "member"):
        return True
    return member.status == "restricted" and bool(getattr(member, "is_member", False))


def _is_configured_channel(chat: types.Chat) -> bool:
    for chat_id, link in (
        (config.channel_1_id, config.channel_1_link),
        (config.channel_2_id, config.channel_2_link),
    ):
        for candidate in _channel_candidates(chat_id, link):
            if candidate.startswith("@"):
                if chat.username and candidate[1:].casefold() == chat.username.casefold():
                    return True
            elif candidate == str(chat.id):
                return True
    return False


@router.chat_member()
async def on_channel_member_update(event: types.ChatMemberUpdated) -> None:
    """Automatically verify a user as soon as they join both required channels."""
    if not _is_configured_channel(event.chat):
        return

    user = event.new_chat_member.user
    if user.is_bot or not _is_member_status(event.new_chat_member):
        return
    if _is_member_status(event.old_chat_member):
        return

    stored_user = await get_user(user.id)
    if not stored_user or stored_user.is_banned or stored_user.verified:
        return

    ch1_ok = await is_channel_member(
        event.bot, config.channel_1_id, user.id, config.channel_1_link
    )
    ch2_ok = await is_channel_member(
        event.bot, config.channel_2_id, user.id, config.channel_2_link
    )
    if ch1_ok is not True or ch2_ok is not True:
        return

    referrer_id = await mark_verified(user.id)
    if referrer_id is None:
        return
    if referrer_id:
        await increment_successful_ref(referrer_id)
        referrer = await get_user(referrer_id)
        verified_user = await get_user(user.id)
        if referrer and verified_user:
            await notify_referral_success(event.bot, referrer, verified_user)

    try:
        await event.bot.send_message(
            user.id,
            WELCOME_MAIN.format(name=escape(display_name(user))),
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Auto-verification welcome could not be sent to user %s", user.id)


# ------------------------- BACK -------------------------
@router.callback_query(F.data == "user_back")
async def cb_back(call: CallbackQuery) -> None:
    user = await get_user(call.from_user.id)
    if not user or not user.verified:
        await call.answer("Please verify first.", show_alert=True)
        return
    await call.message.edit_text(
        "<b>Zonex X Bot</b>\n━━━━━━━━━━━━━━━━━━\n\nChoose an option below:",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "cancel_action")
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await call.message.edit_text("❌ Cancelled.", reply_markup=main_menu_kb())
    except TelegramBadRequest as e:
        if "there is no text in the message to edit" not in str(e).lower():
            raise
        try:
            await call.message.edit_caption(
                caption="❌ Cancelled.",
                reply_markup=main_menu_kb(),
            )
        except TelegramBadRequest:
            await call.message.answer("❌ Cancelled.", reply_markup=main_menu_kb())
    await call.answer()


# ------------------------- ACCOUNT / STATS / SUPPORT -------------------------
@router.callback_query(F.data == "user_account")
async def cb_account(call: CallbackQuery) -> None:
    u = await get_user(call.from_user.id)
    if not u:
        await call.answer("User not found.", show_alert=True)
        return
    text = (
        "👤 <b>My Account</b>\n\n"
        f"🆔 Telegram ID: <code>{u.telegram_id}</code>\n"
        f"👤 Username: @{u.username or '—'}\n"
        f"📅 Joined: {u.joined_at.strftime('%Y-%m-%d')}\n"
        f"✅ Verified: {'Yes' if u.verified else 'No'}\n"
        f"🚫 Banned: {'Yes' if u.is_banned else 'No'}"
    )
    await call.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "user_stats")
async def cb_stats(call: CallbackQuery) -> None:
    u = await get_user(call.from_user.id)
    if not u:
        await call.answer("User not found.", show_alert=True)
        return
    text = (
        "📊 <b>My Stats</b>\n\n"
        f"👥 Total Referrals: {u.total_referrals}\n"
        f"✅ Successful Referrals: {u.successful_referrals}\n"
        f"📱 TG Purchases: {u.tg_purchases}\n"
        f"🛒 Panel Purchases: {u.panel_purchases}"
    )
    await call.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "user_support")
async def cb_support(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "📞 <b>Support</b>\n\nContact admin for any issues.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await call.answer()


# ------------------------- REFERRAL -------------------------
@router.callback_query(F.data == "user_referral")
async def cb_referral(call: CallbackQuery) -> None:
    u = await get_user(call.from_user.id)
    if not u:
        await call.answer("User not found.", show_alert=True)
        return
    link = await create_start_link(call.bot, f"ref_{u.telegram_id}", encode=False)
    tg_req = int(await get_setting("referral_tg_requirement", "15"))
    panel_req = int(await get_setting("referral_panel_requirement", "10"))
    text = (
        "🎁 <b>Referral Program</b>\n\n"
        f"Your referral link:\n<code>{link}</code>\n\n"
        "Is link ko copy karke apne friends ko bhejo. "
        "Friend is link se bot join karke verify karega to referral count hoga.\n\n"
        f"👥 Successful Referrals: {u.successful_referrals}\n\n"
        f"📱 TG Reward: {u.successful_referrals}/{tg_req}\n"
        f"🛒 Panel Reward: {u.successful_referrals}/{panel_req}"
    )
    kb = InlineKeyboardBuilder()
    share_text = "Zonex X Bot join karo aur rewards pao!"
    share_url = (
        "https://t.me/share/url"
        f"?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    )
    kb.button(text="📤 Share with Friends", url=share_url)
    kb.button(text="🔙 Back", callback_data="user_back")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


# ------------------------- TG MENU -------------------------
@router.callback_query(F.data == "user_tg")
async def cb_tg(call: CallbackQuery) -> None:
    await call.answer()
    tg_req = int(await get_setting("referral_tg_requirement", "15"))
    await call.message.edit_text(
        "<b>Zonex X Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📱 <b>TG lene ke do tareeke hain:</b>\n\n"
        f"1️⃣ <b>{tg_req} referrals</b> karo → 1 TG FREE milega\n"
        "2️⃣ <b>Buy TG</b> karke TG purchase karo\n\n"
        "Neeche apna option choose karo:",
        reply_markup=tg_menu_kb(tg_req),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "tg_referral")
async def cb_tg_ref(call: CallbackQuery) -> None:
    u = await get_user(call.from_user.id)
    if not u:
        await call.answer("User not found.", show_alert=True)
        return
    req = int(await get_setting("referral_tg_requirement", "15"))
    eligible = await can_claim_tg_reward(call.from_user.id)
    link = await create_start_link(call.bot, f"ref_{u.telegram_id}", encode=False)
    share_url = (
        "https://t.me/share/url"
        f"?url={quote(link, safe='')}"
        f"&text={quote('Zonex X Bot join karo aur free TG reward pao!', safe='')}"
    )
    text = (
        "🎁 <b>Free TG via Referral</b>\n\n"
        "Apne friends ko ye referral link bhejo:\n"
        f"<code>{link}</code>\n\n"
        "Friend is link se bot open karke required channels join karega, "
        "to referral count hoga.\n\n"
        f"Your referrals: {u.successful_referrals}/{req}\n\n"
        f"{req} successful referrals = 1 TG FREE"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Share Referral Link", url=share_url)
    if eligible:
        text += "\n\n✅ You are eligible!"
        kb.button(text="🎁 Claim Free TG", callback_data="claim_tg")
    kb.button(text="🔙 Back", callback_data="user_tg")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "claim_tg")
async def cb_claim_tg(call: CallbackQuery) -> None:
    if not await record_reward(call.from_user.id, "tg"):
        await call.answer("Not eligible yet, or this reward was already claimed.", show_alert=True)
        return
    await call.answer("✅ Claimed! Admin will deliver.", show_alert=True)
    await send_to_admins(
        call.bot,
        f"🎁 TG referral reward claim\nUser: @{call.from_user.username or call.from_user.id}\nID: <code>{call.from_user.id}</code>",
    )


# ------------------------- BUY TG -------------------------
@router.callback_query(F.data == "tg_buy")
async def cb_tg_buy(call: CallbackQuery) -> None:
    pkgs = await list_tg_packages()
    if not pkgs:
        await call.answer("No TG packages available.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for p in pkgs:
        kb.button(text=f"TG × {p.quantity} — ₹{p.price:.0f}", callback_data=f"tg_buy_{p.id}")
    kb.button(text="🔙 Back", callback_data="user_tg")
    kb.adjust(1)
    text = "📱 <b>TG Packages</b>\n\n" + "\n".join(
        f"{i+1}️⃣ TG × {p.quantity} — ₹{p.price:.0f}" for i, p in enumerate(pkgs)
    )
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("tg_buy_"))
async def cb_tg_buy_pkg(call: CallbackQuery) -> None:
    pid = int(call.data.split("_")[-1])
    pkg = await get_tg_package(pid)
    if not pkg or not pkg.is_active:
        await call.answer("Unavailable.", show_alert=True)
        return

    order_id = gen_order_id()
    await create_order(order_id, call.from_user.id, "TG", pid, pkg.name, pkg.quantity, pkg.price)

    details = await get_payment_details()
    upi_link = build_upi_link(details["upi_id"], "Zonex X Bot", pkg.price, order_id)

    text = (
        "🧾 <b>Order Details</b>\n\n"
        f"Package: {pkg.name}\n"
        f"Price: ₹{pkg.price:.2f}\n"
        f"Order ID: <code>{order_id}</code>\n\n"
        f"<b>Payment Instructions</b>\n{details['instructions']}\n\n"
        f"UPI ID: <code>{details['upi_id']}</code>"
    )
    kb = InlineKeyboardBuilder()
    # Telegram does not allow the upi:// protocol in inline button URLs.
    # The same UPI link is embedded in the QR image below.
    kb.button(text="📤 Submit Payment", callback_data=f"tg_submit_{order_id}")
    kb.button(text="❌ Cancel", callback_data="cancel_action")
    kb.adjust(1)

    if not details["upi_id"]:
        text += "\n\n⚠️ <b>UPI abhi configure nahi hai.</b> Admin Payment Settings mein UPI ID set kare."
        await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        try:
            qr = details["qr_file_id"] or generate_qr(upi_link)
            await call.message.answer_photo(qr, caption=text, reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception:
            await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("tg_submit_"))
async def cb_tg_submit(call: CallbackQuery, state: FSMContext) -> None:
    order_id = call.data.replace("tg_submit_", "")
    o = await get_order(order_id)
    if not o or o.user_id != call.from_user.id:
        await call.answer("Invalid order.", show_alert=True)
        return
    await state.update_data(order_id=order_id)
    await state.set_state(TgStates.waiting_proof)
    await call.message.answer("📤 Send your payment reference / screenshot now.", reply_markup=cancel_kb())
    await call.answer()


@router.message(TgStates.waiting_proof)
async def msg_tg_proof(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    proof_text = (msg.text or "").strip()
    proof_photo = msg.photo[-1].file_id if msg.photo else ""
    proof = proof_text or proof_photo
    if not proof:
        await msg.answer("Send a payment reference as text or attach a screenshot.")
        return
    o = await get_order(order_id) if order_id else None
    if not o or o.user_id != msg.from_user.id:
        await msg.answer("Order not found.", reply_markup=main_menu_kb())
        await state.clear()
        return
    if not await submit_payment_proof(order_id, msg.from_user.id, o.amount, proof):
        await msg.answer("This order was already submitted or is no longer pending.", reply_markup=main_menu_kb())
        await state.clear()
        return

    text = (
        "🔔 <b>New TG Purchase</b>\n\n"
        f"User: @{escape(msg.from_user.username or str(msg.from_user.id))}\n"
        f"User ID: <code>{msg.from_user.id}</code>\n"
        f"Package: {escape(o.product_name)}\n"
        f"Amount: ₹{o.amount:.2f}\n"
        f"Order ID: <code>{order_id}</code>\n"
        "Status: PENDING"
    )
    await send_payment_review(msg.bot, text, order_id, proof_text, proof_photo)
    await msg.answer("✅ Payment submitted! Await admin approval.", reply_markup=main_menu_kb())
    await state.clear()


# ------------------------- PANEL MENU -------------------------
@router.callback_query(F.data == "user_panel")
async def cb_panel(call: CallbackQuery) -> None:
    await call.answer()
    panel_req = int(await get_setting("referral_panel_requirement", "10"))
    await call.message.edit_text(
        "<b>Zonex X Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🛒 <b>Panel lene ke do tareeke hain:</b>\n\n"
        f"1️⃣ <b>{panel_req} referrals</b> karo → 1 Panel FREE milega\n"
        "2️⃣ <b>Buy Panel</b> karke panel purchase karo\n\n"
        "Neeche apna option choose karo:",
        reply_markup=panel_options_kb(panel_req),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "panel_buy")
async def cb_panel_buy(call: CallbackQuery) -> None:
    panels = await list_panels()
    if not panels:
        await call.answer("No panels available.", show_alert=True)
        return
    await call.message.edit_text(
        "🛒 <b>Buy Panel</b>\n\nChoose a panel:",
        reply_markup=panel_menu_kb(panels),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "panel_referral")
async def cb_panel_ref(call: CallbackQuery) -> None:
    u = await get_user(call.from_user.id)
    if not u:
        await call.answer("User not found.", show_alert=True)
        return
    req = int(await get_setting("referral_panel_requirement", "10"))
    eligible = await can_claim_panel_reward(call.from_user.id)
    link = await create_start_link(call.bot, f"ref_{u.telegram_id}", encode=False)
    share_url = (
        "https://t.me/share/url"
        f"?url={quote(link, safe='')}"
        f"&text={quote('Zonex X Bot join karo aur free Panel reward pao!', safe='')}"
    )
    text = (
        "🎁 <b>Free Panel via Referral</b>\n\n"
        "Apne friends ko ye referral link bhejo:\n"
        f"<code>{link}</code>\n\n"
        "Friend is link se bot open karke required channels join karega, "
        "to referral count hoga.\n\n"
        f"Your referrals: {u.successful_referrals}/{req}\n\n"
        f"{req} successful referrals = 1 FREE Panel"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Share Referral Link", url=share_url)
    if eligible:
        kb.button(text="🎁 Claim Free Panel", callback_data="claim_panel")
    kb.button(text="🔙 Back", callback_data="user_panel")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "claim_panel")
async def cb_claim_panel(call: CallbackQuery) -> None:
    if not await record_reward(call.from_user.id, "panel"):
        await call.answer("Not eligible yet, or this reward was already claimed.", show_alert=True)
        return
    await call.answer("✅ Claimed! Admin will deliver.", show_alert=True)
    await send_to_admins(
        call.bot,
        f"🎁 Panel referral reward claim\nUser: @{call.from_user.username or call.from_user.id}\nID: <code>{call.from_user.id}</code>",
    )


@router.callback_query(F.data.startswith("panel_select_"))
async def cb_panel_select(call: CallbackQuery) -> None:
    pid = int(call.data.split("_")[-1])
    p = await get_panel(pid)
    if not p or not p.is_active:
        await call.answer("Unavailable.", show_alert=True)
        return
    if p.stock <= 0 and (not p.credentials or p.credentials == MANUAL_DELIVERY_SENTINEL):
        await call.answer("⚠️ Panel out of stock. Contact admin.", show_alert=True)
        return

    order_id = gen_order_id()
    await create_order(order_id, call.from_user.id, "PANEL", pid, p.name, 1, p.price)

    details = await get_payment_details()
    upi_link = build_upi_link(details["upi_id"], "Zonex X Bot", p.price, order_id)

    text = (
        "🧾 <b>Panel Order</b>\n\n"
        f"Panel: {p.name}\n"
        f"Price: ₹{p.price:.2f}\n"
        f"Order ID: <code>{order_id}</code>\n\n"
        f"<b>Payment Instructions</b>\n{details['instructions']}\n\n"
        f"UPI ID: <code>{details['upi_id']}</code>"
    )
    kb = InlineKeyboardBuilder()
    # Telegram does not allow the upi:// protocol in inline button URLs.
    # The same UPI link is embedded in the QR image below.
    kb.button(text="📤 Submit Payment", callback_data=f"panel_submit_{order_id}")
    kb.button(text="❌ Cancel", callback_data="cancel_action")
    kb.adjust(1)

    if not details["upi_id"]:
        text += "\n\n⚠️ <b>UPI abhi configure nahi hai.</b> Admin Payment Settings mein UPI ID set kare."
        await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    else:
        try:
            qr = details["qr_file_id"] or generate_qr(upi_link)
            await call.message.answer_photo(qr, caption=text, reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception:
            await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("panel_submit_"))
async def cb_panel_submit(call: CallbackQuery, state: FSMContext) -> None:
    order_id = call.data.replace("panel_submit_", "")
    o = await get_order(order_id)
    if not o or o.user_id != call.from_user.id:
        await call.answer("Invalid order.", show_alert=True)
        return
    await state.update_data(order_id=order_id)
    await state.set_state(PanelStates.waiting_proof)
    await call.message.answer("📤 Send your payment reference / screenshot now.", reply_markup=cancel_kb())
    await call.answer()


@router.message(PanelStates.waiting_proof)
async def msg_panel_proof(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    proof_text = (msg.text or "").strip()
    proof_photo = msg.photo[-1].file_id if msg.photo else ""
    proof = proof_text or proof_photo
    if not proof:
        await msg.answer("Send a payment reference as text or attach a screenshot.")
        return
    o = await get_order(order_id) if order_id else None
    if not o or o.user_id != msg.from_user.id:
        await msg.answer("Order not found.", reply_markup=main_menu_kb())
        await state.clear()
        return
    if not await submit_payment_proof(order_id, msg.from_user.id, o.amount, proof):
        await msg.answer("This order was already submitted or is no longer pending.", reply_markup=main_menu_kb())
        await state.clear()
        return

    text = (
        "🔔 <b>New Panel Order</b>\n\n"
        f"User: @{escape(msg.from_user.username or str(msg.from_user.id))}\n"
        f"User ID: <code>{msg.from_user.id}</code>\n"
        f"Panel: {escape(o.product_name)}\n"
        f"Amount: ₹{o.amount:.2f}\n"
        f"Order ID: <code>{order_id}</code>\n"
        "Status: PENDING"
    )
    await send_payment_review(msg.bot, text, order_id, proof_text, proof_photo)
    await msg.answer("✅ Payment submitted! Await admin approval.", reply_markup=main_menu_kb())
    await state.clear()


# ------------------------- ADMIN -------------------------
@router.message(Command("getfile", "source"))
async def cmd_getfile(msg: Message) -> None:
    if not msg.from_user or not is_admin(msg.from_user.id):
        await msg.answer("⛔ Access denied.")
        return
    if msg.chat.type != "private":
        await msg.answer("Please send /getfile in a private chat with the bot.")
        return

    source_path = Path(__file__).resolve()
    try:
        source_file = BufferedInputFile(
            source_path.read_bytes(),
            filename=source_path.name,
        )
        await msg.answer_document(
            source_file,
            caption="Complete current bot source file (main.py).",
        )
    except (OSError, TelegramAPIError):
        logger.exception("Could not send the bot source file to admin %s", msg.from_user.id)
        await msg.answer("⚠️ I could not send the source file. Please try again later.")


@router.message(Command("admin"))
async def cmd_admin(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔ Access denied.")
        return
    await msg.answer(
        "━━━━━━━━━━━━━━━━━━\n<b>ADMIN PANEL</b>\n━━━━━━━━━━━━━━━━━━",
        reply_markup=admin_main_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "admin_back")
async def cb_admin_back(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    await call.message.edit_text(
        "━━━━━━━━━━━━━━━━━━\n<b>ADMIN PANEL</b>\n━━━━━━━━━━━━━━━━━━",
        reply_markup=admin_main_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    s = await get_stats()
    text = (
        "📊 <b>Statistics</b>\n\n"
        f"👥 Total Users: {s['total_users']}\n"
        f"✅ Verified: {s['verified']}\n"
        f"🚫 Banned: {s['banned']}\n"
        f"📦 Orders: {s['orders']}\n"
        f"⏳ Pending: {s['pending']}\n"
        f"✅ Approved: {s['approved']}\n"
        f"💰 Revenue: ₹{s['revenue']:.2f}"
    )
    await call.message.edit_text(text, reply_markup=back_admin_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_status")
async def cb_admin_status(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    s = await get_stats()
    maint = await get_setting("maintenance_global", "false")
    text = (
        "🟢 <b>Bot Status</b>\n\n"
        f"🟢 Online\n"
        f"👥 Users: {s['total_users']}\n"
        f"✅ Verified: {s['verified']}\n"
        f"📦 Orders: {s['orders']}\n"
        f"⏳ Pending: {s['pending']}\n"
        f"💰 Revenue: ₹{s['revenue']:.2f}\n"
        f"🛠 Maintenance: {maint}"
    )
    await call.message.edit_text(text, reply_markup=back_admin_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_maintenance")
async def cb_admin_maint(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    cur = await get_setting("maintenance_global", "false")
    new = "false" if cur == "true" else "true"
    await set_setting("maintenance_global", new)
    status = "🛠 ENABLED" if new == "true" else "🟢 DISABLED"
    await call.message.edit_text(
        f"🛠 <b>Maintenance Mode</b>\n\nCurrent: {status}",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )
    await call.answer(f"Maintenance {status}")


@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.broadcast)
    await call.message.edit_text(
        "📢 <b>Broadcast</b>\n\nSend the message to broadcast.",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AdminStates.broadcast)
async def do_broadcast(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    uids = await all_user_ids()
    sent = failed = blocked = 0
    for uid in uids:
        try:
            await msg.copy_to(uid)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            err = str(e).lower()
            if "blocked" in err or "deactivated" in err:
                blocked += 1
            else:
                failed += 1
    await msg.answer(
        f"📢 Broadcast done.\n\n✅ Sent: {sent}\n❌ Failed: {failed}\n🚫 Blocked: {blocked}",
        reply_markup=admin_main_kb(),
    )
    await state.clear()


@router.callback_query(F.data.startswith("approve_"))
async def cb_approve(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    order_id = call.data.replace("approve_", "")
    o = await get_order(order_id)
    if not o or o.status not in ("PENDING", "PAYMENT_SUBMITTED"):
        await call.answer("Already processed.", show_alert=True)
        return

    if not await transition_order(order_id, ("PENDING", "PAYMENT_SUBMITTED"), "APPROVED"):
        await call.answer("Already processed.", show_alert=True)
        return
    await update_payment_status(order_id, "APPROVED", call.from_user.id)

    if o.product_type == "TG":
        note = await get_setting("tg_delivery_note", "✅ TG order approved. Admin will contact you.")
        try:
            await call.bot.send_message(o.user_id, note)
        except Exception as e:
            logger.error("User notify failed: %s", e)
    else:
        p = await get_panel(o.product_id)
        delivered = False
        if p and p.credentials == MANUAL_DELIVERY_SENTINEL:
            if await decrement_panel_stock(p.id):
                await send_to_admins(
                    call.bot,
                    "📦 <b>Manual Panel Delivery Required</b>\n\n"
                    f"User ID: <code>{o.user_id}</code>\n"
                    f"Panel: {escape(o.product_name)}\n"
                    f"Amount: ₹{o.amount:.2f}\n"
                    f"Order ID: <code>{order_id}</code>\n\n"
                    "Payment approved. User ko panel manually bhejo.",
                )
                try:
                    await call.bot.send_message(
                        o.user_id,
                        "✅ Payment approved.\n\n"
                        "Admin ab aapko panel manually deliver karega. "
                        "Please wait for the admin message.",
                    )
                except Exception as e:
                    logger.error("Manual panel customer notification failed: %s", e)
                delivered = True
        elif p:
            if p.credentials and (p.stock <= 0):
                delivery = p.credentials
            elif p.stock > 0:
                ok = await decrement_panel_stock(p.id)
                delivery = p.credentials or p.delivery_info or "No credentials configured." if ok else None
            else:
                delivery = None

            if delivery:
                try:
                    await call.bot.send_message(
                        o.user_id,
                        f"✅ <b>Panel Delivered</b>\n\n{escape(delivery)}",
                        parse_mode="HTML",
                    )
                    await update_order(order_id, "DELIVERED")
                    delivered = True
                except Exception as e:
                    logger.error("Panel delivery failed: %s", e)

        if not delivered:
            try:
                await call.bot.send_message(
                    o.user_id,
                    "⚠️ This panel is currently unavailable. Please contact admin.",
                )
            except Exception:
                pass

    await increment_purchase(o.user_id, o.product_type)
    await call.message.edit_text(f"✅ Order <code>{order_id}</code> approved.", parse_mode="HTML")
    await call.answer("Approved ✅")


@router.callback_query(F.data.startswith("reject_"))
async def cb_reject(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    order_id = call.data.replace("reject_", "")
    o = await get_order(order_id)
    if not o or o.status not in ("PENDING", "PAYMENT_SUBMITTED"):
        await call.answer("Already processed.", show_alert=True)
        return
    if not await transition_order(order_id, ("PENDING", "PAYMENT_SUBMITTED"), "REJECTED"):
        await call.answer("Already processed.", show_alert=True)
        return
    await update_payment_status(order_id, "REJECTED", call.from_user.id)
    try:
        await call.bot.send_message(
            o.user_id,
            f"❌ Your order <code>{order_id}</code> was rejected. Contact support.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await call.message.edit_text(f"❌ Order <code>{order_id}</code> rejected.", parse_mode="HTML")
    await call.answer("Rejected ❌")


@router.callback_query(F.data == "admin_ban_menu")
async def cb_ban_menu(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🚫 Ban User", callback_data="admin_ban")
    kb.button(text="♻️ Unban User", callback_data="admin_unban")
    kb.button(text="🔙 Admin Panel", callback_data="admin_back")
    kb.adjust(1)
    await call.message.edit_text("🚫 <b>Ban / Unban</b>", reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_ban")
async def cb_ban(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.ban_user)
    await call.message.edit_text("Send the Telegram ID to ban:", reply_markup=back_admin_kb())
    await call.answer()


@router.message(AdminStates.ban_user)
async def do_ban(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        uid = int(msg.text.strip())
    except ValueError:
        await msg.answer("Invalid ID.")
        return
    await set_banned(uid, True, "Banned by admin", msg.from_user.id)
    await msg.answer(f"🚫 User <code>{uid}</code> banned.", parse_mode="HTML", reply_markup=admin_main_kb())
    await state.clear()


@router.callback_query(F.data == "admin_unban")
async def cb_unban(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.unban_user)
    await call.message.edit_text("Send the Telegram ID to unban:", reply_markup=back_admin_kb())
    await call.answer()


@router.message(AdminStates.unban_user)
async def do_unban(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        uid = int(msg.text.strip())
    except ValueError:
        await msg.answer("Invalid ID.")
        return
    await set_banned(uid, False)
    await msg.answer(f"♻️ User <code>{uid}</code> unbanned.", parse_mode="HTML", reply_markup=admin_main_kb())
    await state.clear()


@router.callback_query(F.data == "admin_users")
async def cb_users(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.find_user)
    await call.message.edit_text(
        "👥 <b>Find User</b>\n\nSend Telegram ID or @username.",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AdminStates.find_user)
async def do_find_user(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    q = msg.text.strip()
    user = None
    if q.isdigit():
        user = await get_user(int(q))
    elif q.startswith("@"):
        user = await get_user_by_username(q)
    if not user:
        await msg.answer("User not found.")
        return
    text = (
        f"👤 <b>User</b>\n\n"
        f"ID: <code>{user.telegram_id}</code>\n"
        f"Username: @{user.username or '—'}\n"
        f"Name: {user.first_name or '—'}\n"
        f"Joined: {user.joined_at.strftime('%Y-%m-%d')}\n"
        f"Verified: {user.verified}\n"
        f"Total Referrals: {user.total_referrals}\n"
        f"Successful: {user.successful_referrals}\n"
        f"TG Purchases: {user.tg_purchases}\n"
        f"Panel Purchases: {user.panel_purchases}\n"
        f"Banned: {user.is_banned}"
    )
    await msg.answer(text, parse_mode="HTML", reply_markup=admin_main_kb())
    await state.clear()


@router.callback_query(F.data == "admin_referral")
async def cb_ref_settings(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    tg = await get_setting("referral_tg_requirement", "15")
    pn = await get_setting("referral_panel_requirement", "10")
    text = (
        "🎁 <b>Referral Settings</b>\n\n"
        f"📱 TG requirement: {tg}\n"
        f"🛒 Panel requirement: {pn}\n\n"
        "Neeche button se requirement change karo.\n\n"
        "Commands bhi available hain:\n"
        "/set_tg_req &lt;number&gt;\n"
        "/set_panel_req &lt;number&gt;"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⚙️ Set TG Referrals", callback_data="admin_set_tg_req")
    kb.button(text="⚙️ Set Panel Referrals", callback_data="admin_set_panel_req")
    kb.button(text="🔙 Admin Panel", callback_data="admin_back")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_set_tg_req")
async def cb_admin_set_tg_req(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.set_tg_req)
    await call.message.edit_text(
        "⚙️ <b>Set TG Referral Requirement</b>\n\n"
        "Kitne referrals par 1 TG FREE dena hai? Sirf number bhejo.\n"
        "Example: <code>15</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AdminStates.set_tg_req)
async def do_set_tg_req_from_menu(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await msg.answer("❌ 1 ya usse bada valid number bhejo. Example: <code>15</code>", parse_mode="HTML")
        return
    value = int(raw)
    await set_setting("referral_tg_requirement", str(value))
    await msg.answer(
        f"✅ Ab <b>{value} referrals</b> par 1 TG FREE milega.",
        reply_markup=admin_main_kb(),
        parse_mode="HTML",
    )
    await state.clear()


@router.callback_query(F.data == "admin_set_panel_req")
async def cb_admin_set_panel_req(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.set_panel_req)
    await call.message.edit_text(
        "⚙️ <b>Set Panel Referral Requirement</b>\n\n"
        "Kitne referrals par 1 Panel FREE dena hai? Sirf number bhejo.\n"
        "Example: <code>10</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AdminStates.set_panel_req)
async def do_set_panel_req_from_menu(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    raw = (msg.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await msg.answer("❌ 1 ya usse bada valid number bhejo. Example: <code>10</code>", parse_mode="HTML")
        return
    value = int(raw)
    await set_setting("referral_panel_requirement", str(value))
    await msg.answer(
        f"✅ Ab <b>{value} referrals</b> par 1 Panel FREE milega.",
        reply_markup=admin_main_kb(),
        parse_mode="HTML",
    )
    await state.clear()


@router.message(Command("set_tg_req"))
async def cmd_set_tg_req(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        v = int(msg.text.split()[1])
        if v < 1:
            raise ValueError
    except (IndexError, ValueError):
        await msg.answer("Usage: /set_tg_req <number> (number must be at least 1)")
        return
    await set_setting("referral_tg_requirement", str(v))
    await msg.answer(f"✅ TG referral requirement = {v}")


@router.message(Command("set_panel_req"))
async def cmd_set_panel_req(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        v = int(msg.text.split()[1])
        if v < 1:
            raise ValueError
    except (IndexError, ValueError):
        await msg.answer("Usage: /set_panel_req <number> (number must be at least 1)")
        return
    await set_setting("referral_panel_requirement", str(v))
    await msg.answer(f"✅ Panel referral requirement = {v}")


@router.callback_query(F.data == "admin_payment_settings")
async def cb_pay_settings(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    await call.answer()
    upi = await get_setting("payment_upi_id", config.default_upi_id)
    qr_file_id = await get_setting("payment_qr_file_id", "")
    text = (
        "💳 <b>Payment Settings</b>\n\n"
        f"UPI ID: <code>{upi}</code>\n\n"
        f"Custom QR: {'✅ Set' if qr_file_id else '⚪ Auto QR'}\n\n"
        "Buttons se settings change karo, ya commands use karo:\n"
        "/set_upi &lt;id&gt;\n"
        "/set_instructions &lt;text&gt;"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Set UPI ID", callback_data="admin_set_upi")
    kb.button(text="🖼️ Set Custom QR", callback_data="admin_set_qr")
    kb.button(text="♻️ Use Auto QR", callback_data="admin_clear_qr")
    kb.button(text="🔙 Admin Panel", callback_data="admin_back")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "admin_set_upi")
async def cb_admin_set_upi(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await call.answer()
    await state.set_state(AdminStates.set_upi)
    await call.message.edit_text(
        "💳 <b>Set UPI ID</b>\n\n"
        "Apna UPI ID bhejo.\n"
        "Example: <code>zonex@oksbi</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AdminStates.set_upi)
async def do_set_upi_from_menu(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    upi = (msg.text or "").strip()
    if not upi or " " in upi:
        await msg.answer("❌ Valid UPI ID bhejo. Example: <code>zonex@oksbi</code>", parse_mode="HTML")
        return
    await set_setting("payment_upi_id", upi)
    await msg.answer(
        f"✅ UPI ID set ho gaya: <code>{upi}</code>",
        reply_markup=admin_main_kb(),
        parse_mode="HTML",
    )
    await state.clear()


@router.callback_query(F.data == "admin_set_qr")
async def cb_admin_set_qr(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await call.answer()
    await state.set_state(AdminStates.set_qr)
    await call.message.edit_text(
        "🖼️ <b>Set Custom QR</b>\n\n"
        "Apna UPI QR image ke roop mein bhejo.\n"
        "Aage se TG/Panel orders mein yahi QR dikhega.",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )


@router.message(AdminStates.set_qr)
async def do_set_qr_from_menu(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    if not msg.photo:
        await msg.answer("❌ QR ko photo/image ke roop mein bhejo.")
        return
    await set_setting("payment_qr_file_id", msg.photo[-1].file_id)
    await msg.answer(
        "✅ Custom QR save ho gaya. Ab TG/Panel orders mein yahi QR use hoga.",
        reply_markup=admin_main_kb(),
    )
    await state.clear()


@router.callback_query(F.data == "admin_clear_qr")
async def cb_admin_clear_qr(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    await call.answer("Auto QR enabled")
    await set_setting("payment_qr_file_id", "")
    await call.message.edit_text(
        "♻️ <b>Auto QR enabled</b>\n\nAb QR order amount aur UPI ID se automatically generate hoga.",
        reply_markup=back_admin_kb(),
        parse_mode="HTML",
    )


@router.message(Command("set_upi"))
async def cmd_set_upi(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /set_upi <id>")
        return
    await set_setting("payment_upi_id", parts[1].strip())
    await msg.answer("✅ UPI ID updated.")


@router.message(Command("set_instructions"))
async def cmd_set_instructions(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /set_instructions <text>")
        return
    await set_setting("payment_instructions", parts[1].strip())
    await msg.answer("✅ Instructions updated.")


@router.callback_query(F.data == "admin_panels")
async def cb_admin_panels(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    panels = await list_panels(active_only=False)
    text = "🛒 <b>Panels</b>\n\n"
    if panels:
        text += "\n".join(
            f"#{p.id} {p.name} — ₹{p.price:.0f} | Stock: {p.stock} | {'🟢' if p.is_active else '🔴'}"
            for p in panels
        )
    else:
        text += "None configured."
    text += (
        "\n\nCommands:\n"
        "/add_panel Name | Price | DeliveryInfo | Credentials | Stock\n"
        "/del_panel &lt;id&gt;\n"
        "/toggle_panel &lt;id&gt;"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Add Panel", callback_data="admin_add_panel")
    kb.button(text="🔙 Admin Panel", callback_data="admin_back")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_add_panel")
async def cb_admin_add_panel(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.add_panel)
    await call.message.edit_text(
        "➕ <b>Add Panel</b>\n\n"
        "Is format mein ek line bhejo:\n"
        "<code>Name | Price | DeliveryInfo | Credentials | Stock</code>\n\n"
        "Example:\n"
        "<code>Premium Panel | 299 | Login at example.com | user:demo pass:demo123 | 5</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AdminStates.add_panel)
async def do_add_panel_from_menu(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        parts = [x.strip() for x in (msg.text or "").split("|")]
        if len(parts) != 5:
            raise ValueError
        name, price, delivery_info, credentials, stock = parts
        if not name or not price or not stock:
            raise ValueError
        async with db_session() as s:
            s.add(
                Panel(
                    name=name,
                    price=float(price),
                    delivery_info=delivery_info,
                    credentials=credentials,
                    stock=int(stock),
                )
            )
        await msg.answer(
            f"✅ Panel '<b>{name}</b>' add ho gaya.\n"
            f"Price: ₹{float(price):.2f}\n"
            f"Stock: {int(stock)}",
            reply_markup=admin_main_kb(),
            parse_mode="HTML",
        )
        await state.clear()
    except (TypeError, ValueError):
        await msg.answer(
            "❌ Format galat hai.\n\n"
            "Is format mein bhejo:\n"
            "<code>Name | Price | DeliveryInfo | Credentials | Stock</code>\n\n"
            "Example:\n"
            "<code>Premium Panel | 299 | Login at example.com | user:demo pass:demo123 | 5</code>",
            parse_mode="HTML",
        )


@router.message(Command("add_panel"))
async def cmd_add_panel(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /add_panel Name | Price | DeliveryInfo | Credentials | Stock")
        return
    try:
        parts2 = [x.strip() for x in parts[1].split("|")]
        if len(parts2) != 5:
            raise ValueError("Need 5 parts")
        name, price, dinfo, cred, stock = parts2
        async with db_session() as s:
            s.add(Panel(
                name=name, price=float(price), delivery_info=dinfo,
                credentials=cred, stock=int(stock),
            ))
        await msg.answer(f"✅ Panel '{name}' added.")
    except Exception as e:
        await msg.answer(f"Error: {e}")


@router.message(Command("del_panel"))
async def cmd_del_panel(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        pid = int(msg.text.split()[1])
    except (IndexError, ValueError):
        await msg.answer("Usage: /del_panel <id>")
        return
    async with db_session() as s:
        p = await s.get(Panel, pid)
        if p:
            await s.delete(p)
    await msg.answer(f"✅ Panel {pid} deleted.")


@router.message(Command("toggle_panel"))
async def cmd_toggle_panel(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        pid = int(msg.text.split()[1])
    except (IndexError, ValueError):
        await msg.answer("Usage: /toggle_panel <id>")
        return
    async with db_session() as s:
        p = await s.get(Panel, pid)
        if p:
            p.is_active = not p.is_active
            await msg.answer(f"Panel {pid} is now {'🟢 active' if p.is_active else '🔴 inactive'}.")
        else:
            await msg.answer("Panel not found.")


@router.callback_query(F.data == "admin_tg")
async def cb_admin_tg(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    pkgs = await list_tg_packages(active_only=False)
    text = "📱 <b>TG Packages</b>\n\n"
    if pkgs:
        text += "\n".join(
            f"#{p.id} {p.name} | Qty: {p.quantity} | ₹{p.price:.0f} | {'🟢' if p.is_active else '🔴'}"
            for p in pkgs
        )
    else:
        text += "None configured."
    text += (
        "\n\nCommands:\n"
        "/add_tg Name/Quantity/Price\n"
        "/del_tg &lt;id&gt;\n"
        "/toggle_tg &lt;id&gt;"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Add TG Package", callback_data="admin_add_tg")
    kb.button(text="🔙 Admin Panel", callback_data="admin_back")
    kb.adjust(1)
    await call.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_add_tg")
async def cb_admin_add_tg(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.add_tg)
    await call.message.edit_text(
        "➕ <b>Add TG Package</b>\n\n"
        "Is format mein ek line bhejo:\n"
        "<code>Name/Quantity/Price</code>\n\n"
        "Example:\n"
        "<code>100TG/100/50</code>\n\n"
        "Pipe format bhi chalega: <code>100 TG | 100 | 50</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(AdminStates.add_tg)
async def do_add_tg_from_menu(msg: Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        name, qty, price = parse_tg_package_input(msg.text or "")
        async with db_session() as s:
            s.add(TgPackage(name=name, quantity=int(qty), price=float(price)))
        await msg.answer(
            f"✅ TG package '<b>{name}</b>' added.\n"
            f"Quantity: {int(qty)}\nPrice: ₹{float(price):.2f}",
            reply_markup=admin_main_kb(),
            parse_mode="HTML",
        )
        await state.clear()
    except Exception:
        await msg.answer(
            "❌ Format galat hai.\n\n"
            "Example:\n"
            "<code>100TG/100/50</code>",
            parse_mode="HTML",
        )


@router.message(Command("add_tg"))
async def cmd_add_tg(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Usage: /add_tg Name | Quantity | Price")
        return
    try:
        name, qty, price = parse_tg_package_input(parts[1])
        async with db_session() as s:
            s.add(TgPackage(name=name, quantity=int(qty), price=float(price)))
        await msg.answer(f"✅ TG '{name}' added.")
    except Exception as e:
        await msg.answer(f"Error: {e}")


@router.message(Command("del_tg"))
async def cmd_del_tg(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        tid = int(msg.text.split()[1])
    except (IndexError, ValueError):
        await msg.answer("Usage: /del_tg <id>")
        return
    async with db_session() as s:
        p = await s.get(TgPackage, tid)
        if p:
            await s.delete(p)
    await msg.answer(f"✅ TG package {tid} deleted.")


@router.message(Command("toggle_tg"))
async def cmd_toggle_tg(msg: Message) -> None:
    if not is_admin(msg.from_user.id):
        return
    try:
        tid = int(msg.text.split()[1])
    except (IndexError, ValueError):
        await msg.answer("Usage: /toggle_tg <id>")
        return
    async with db_session() as s:
        p = await s.get(TgPackage, tid)
        if p:
            p.is_active = not p.is_active
            await msg.answer(f"TG package {tid} is now {'🟢' if p.is_active else '🔴'}.")


@router.callback_query(F.data == "admin_channels")
async def cb_channels(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    text = (
        "🔗 <b>Channel Settings</b>\n\n"
        f"Channel 1: {config.channel_1_id}\n"
        f"Channel 2: {config.channel_2_id}\n\n"
        "Configured via .env file."
    )
    await call.message.edit_text(text, reply_markup=back_admin_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "admin_payments")
async def cb_admin_payments(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        return
    s = await get_stats()
    text = (
        "💰 <b>Payments</b>\n\n"
        f"⏳ Pending orders: {s['pending']}\n"
        f"✅ Approved orders: {s['approved']}\n"
        f"💰 Total revenue: ₹{s['revenue']:.2f}\n\n"
        "Approve/Reject via the buttons sent when user submits payment."
    )
    await call.message.edit_text(text, reply_markup=back_admin_kb(), parse_mode="HTML")
    await call.answer()


# ============================================================
# GLOBAL MAINTENANCE + BAN CHECK
# ============================================================
@router.message(F.text)
async def catch_text(msg: Message) -> None:
    # Only acts as fallback for unknown text; skip commands
    if msg.text and msg.text.startswith("/"):
        return
    maint = await get_setting("maintenance_global", "false")
    if maint == "true" and not is_admin(msg.from_user.id):
        await msg.answer("🛠 Zonex X Bot is currently under maintenance. Please try again later.")
        return
    await msg.answer("Use /start to see the menu.")


# ============================================================
# ENTRYPOINT
# ============================================================
async def main() -> None:
    logger.info("Starting Zonex X Bot...")
    bot: Optional[Bot] = None
    try:
        await init_db()
        bot = Bot(
            token=config.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(router)

        # Check the token and Telegram connectivity before entering polling.
        bot_info = await bot.get_me()
        logger.info("Telegram connection verified for @%s", bot_info.username)
        # Keep queued user updates across Railway restarts.
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Starting Telegram long polling...")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
        )
    except Exception:
        logger.exception("Bot failed during startup or while running")
        raise
    finally:
        if bot is not None:
            await bot.session.close()
        await engine.dispose()
        logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown requested.")
