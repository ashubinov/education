"""Схемы запросов (Pydantic): все данные от фронтенда проходят проверку типов и длин до попадания в логику."""
from datetime import date as Date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

LOGIN_RE = r"^[\w.\-]{3,32}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class RegisterIn(Strict):
    username: str = Field(pattern=LOGIN_RE, description="3–32 символа: буквы, цифры, . _ -")
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field("", max_length=40)


class LoginIn(Strict):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeIn(Strict):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class ThemeExtra(Strict):
    """Дополнительные настройки оформления (цвет и светлая/тёмная тема хранятся отдельно)."""
    preset: str = Field("", max_length=20, pattern=r"^[a-z0-9_-]*$")
    bg: Literal["glow", "solid", "tint", "amoled"] = "glow"
    radius: Literal["sharp", "normal", "round"] = "normal"
    motion: Literal["full", "reduced"] = "full"


THEME_DEFAULT = ThemeExtra().model_dump()


class MeUpdate(Strict):
    display_name: Optional[str] = Field(None, min_length=1, max_length=40)
    avatar: Optional[str] = Field(None, min_length=1, max_length=8)
    theme_color: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    theme_mode: Optional[Literal["dark", "light", "auto"]] = None
    theme_extra: Optional[ThemeExtra] = None
    sound: Optional[bool] = None
    reminders_on: Optional[bool] = None
    reminder_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsUpdate(Strict):
    openrouter_key: Optional[str] = Field(None, max_length=300)
    deepseek_key: Optional[str] = Field(None, max_length=300)
    llm_models: Optional[str] = Field(None, max_length=600)
    llm_paid: Optional[bool] = None
    llm_base_url: Optional[str] = Field(None, max_length=300)
    llm_api_key: Optional[str] = Field(None, max_length=300)
    llm_custom_models: Optional[str] = Field(None, max_length=600)
    telegram_token: Optional[str] = Field(None, max_length=200)

    @field_validator("llm_base_url")
    @classmethod
    def _url(cls, v):
        if v and not v.startswith(("https://", "http://")):
            raise ValueError("адрес должен начинаться с https://")
        return v


class GoalIn(Strict):
    date: Optional[Date] = None


class AnswerIn(Strict):
    choice: Optional[int | list[int]] = None
    text: Optional[str] = Field(None, max_length=8000)
    idx: Optional[int] = Field(None, ge=0, le=500)
    self_score: Optional[float] = Field(None, alias="self", ge=0, le=1)
    skip: Optional[bool] = None

    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=False)

    def to_payload(self) -> dict:
        d = {}
        if self.choice is not None:
            d["choice"] = self.choice
        if self.text is not None:
            d["text"] = self.text
        if self.idx is not None:
            d["idx"] = self.idx
        if self.self_score is not None:
            d["self"] = self.self_score
        if self.skip:
            d["skip"] = True
        return d


class StartIn(Strict):
    restart: bool = False


class PublishIn(Strict):
    title: Optional[str] = Field(None, max_length=100)


class FriendRequestIn(Strict):
    username: str = Field(min_length=1, max_length=64)


class SignatureIn(Strict):
    text: str = Field(min_length=1, max_length=200)  # точный предел (40 символов) и очистку проверяет social.clean_signature с понятным сообщением


class BanIn(Strict):
    reason: str = Field("", max_length=200)


class UsernameChangeIn(Strict):
    username: str = Field(pattern=LOGIN_RE, description="3–32 символа: буквы, цифры, . _ -")
    password: str = Field(min_length=1, max_length=128)


class SlotSpinIn(Strict):
    bet: int = Field(ge=1, le=100000)
    request_id: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_-]{8,64}$", description="уникальный номер вращения от клиента: повтор запроса не спишет ставку второй раз")


class SlotBuyIn(Strict):
    chips: int = Field(ge=1, le=100000)
    request_id: Optional[str] = Field(None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
