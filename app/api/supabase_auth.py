"""
Supabase Authentication API endpoints.
Production-ready auth using Supabase only.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.supabase_auth import supabase_auth_service, get_current_supabase_user
from app.core.auth import get_supabase_client
from app.schemas.schemas import (
    UserCreate,
    UserLogin,
    UserResponse,
    TokenResponse
)
from app.core.logging import logger

router = APIRouter(prefix="/auth", tags=["Authentication"])


# BUG-07 FIX: Refresh token must travel in the request body, never as a
# query parameter — query params are logged by every proxy and CDN.
class RefreshRequest(BaseModel):
    refresh_token: str

# A3 FIX: /register does NOT use the db session — remove it to avoid
# wasting a connection from the pool on every registration call.
@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register_supabase(
    user_data: UserCreate,
) -> UserResponse:
    """Register a new user using Supabase."""
    
    try:
        result = await supabase_auth_service.sign_up(
            email=user_data.email,
            password=user_data.password,
            full_name=user_data.full_name,
            role=user_data.role
        )
        
        if result.get('user'):
            logger.info(f"User registered via Supabase: {user_data.email}")
            return UserResponse(
                id=result['user']['id'],
                email=result['user']['email'],
                full_name=user_data.full_name,
                role=user_data.role,
                is_active=True,
                created_at=result['user'].get('created_at')
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Registration failed"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration error: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Registration failed: {str(e)}"
        )

# A3 FIX: /login does NOT use the db session.
@router.post("/login", response_model=TokenResponse)
async def login_supabase(
    credentials: UserLogin,
) -> TokenResponse:
    """Authenticate user using Supabase."""
    
    try:
        result = await supabase_auth_service.sign_in(
            email=credentials.email,
            password=credentials.password
        )
        
        if result.get('access_token'):
            logger.info(f"User logged in via Supabase: {credentials.email}")
            
            return TokenResponse(
                access_token=result['access_token'],
                refresh_token=result.get('refresh_token', ''),
                token_type="bearer",
                expires_in=3600
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {str(e)}"
        )

# BUG-07 FIX: Accept refresh_token in the JSON body, not as a query param.
@router.post("/refresh", response_model=TokenResponse)
async def refresh_token_supabase(
    body: RefreshRequest,
) -> TokenResponse:
    """Refresh access token using Supabase."""
    
    try:
        result = await supabase_auth_service.refresh_token(body.refresh_token)
        
        if result.get('access_token'):
            logger.info("Token refreshed via Supabase")
            
            return TokenResponse(
                access_token=result['access_token'],
                refresh_token=result.get('refresh_token', ''),
                token_type="bearer",
                expires_in=3600
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not refresh token"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Token refresh failed: {str(e)}"
        )

@router.get("/me", response_model=UserResponse)
async def get_me_supabase(
    current_user: dict = Depends(get_current_supabase_user)
) -> UserResponse:
    """Get current authenticated user."""
    
    try:
        # BUG-11 FIX: current_user.get('id') may return None in edge cases
        # (e.g. Supabase proxy responses without an 'id' key).  Fall back to
        # the JWT 'sub' claim which always identifies the user.
        return UserResponse(
            id=current_user.get('id') or current_user.get('sub', 'unknown'),
            email=current_user.get('email'),
            full_name=current_user.get('full_name') or current_user.get('email', '').split('@')[0] or 'User',
            role=current_user.get('role', 'candidate'),
            is_active=current_user.get('is_active', True),
            created_at=current_user.get('created_at')
        )
    except Exception as e:
        logger.error(f"Get user error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user information"
        )

# A2 FIX: /logout must invalidate the Supabase session server-side.
# The previous implementation only logged the action locally — the
# access token remained valid for its full 1-hour lifetime.
@router.post("/logout")
async def logout_supabase(
    current_user: dict = Depends(get_current_supabase_user)
):
    """Logout user from Supabase — invalidates the server-side session."""
    
    try:
        user_id = current_user.get('id') or current_user.get('sub')
        if user_id:
            supabase = get_supabase_client()
            # admin.sign_out revokes the refresh token server-side so the
            # user cannot obtain a new access token after logout.
            await __import__('asyncio').to_thread(
                supabase.auth.admin.sign_out, str(user_id)
            )
        logger.info(f"User logged out (server-side session revoked): {current_user.get('email')}")
        return {"message": "Successfully logged out"}
        
    except Exception as e:
        logger.error(f"Logout error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Logout failed"
        )
