import { SessionOptions } from 'iron-session';

export interface SessionData {
  accessToken?: string;
  refreshToken?: string;
  expiresAt?: number; // unix ms
  userName?: string;
  userEmail?: string;
}

export const sessionOptions: SessionOptions = {
  password: process.env.SESSION_SECRET!,
  cookieName: 'box_idx_session',
  cookieOptions: {
    secure: process.env.NODE_ENV === 'production',
    httpOnly: true,
    sameSite: 'lax',
    maxAge: 60 * 60 * 24, // 1 day
  },
};
