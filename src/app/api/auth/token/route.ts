import { NextResponse } from 'next/server';
import { getIronSession } from 'iron-session';
import { cookies } from 'next/headers';
import { sessionOptions, SessionData } from '@/lib/session';
import { getFreshToken } from '@/lib/box';

export async function GET() {
  const session = await getIronSession<SessionData>(cookies(), sessionOptions);

  if (!session.accessToken) {
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401 });
  }

  try {
    const token = await getFreshToken(session);
    await session.save(); // persist any refresh
    return NextResponse.json({
      accessToken: token,
      userName: session.userName,
      userEmail: session.userEmail,
    });
  } catch {
    return NextResponse.json({ error: 'Token refresh failed' }, { status: 401 });
  }
}
