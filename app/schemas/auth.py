from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: str
    password: str
    preferred_language: str = "en"


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    preferred_language: str
    streak_days: int

    model_config = {"from_attributes": True}


class RefreshRequest(BaseModel):
    refresh_token: str
