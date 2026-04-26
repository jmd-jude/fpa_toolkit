import { NextRequest, NextResponse } from 'next/server';
import { Pool } from 'pg';

let pool: Pool | null = null;

function getPool(): Pool {
  const url = process.env.DATABASE_URL;
  if (!url) throw new Error('DATABASE_URL not set');
  if (!pool) pool = new Pool({ connectionString: url, ssl: false });
  return pool;
}

const EDIT_PASSWORD = 'fpamedit';

const CORS: Record<string, string> = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

interface KanbanCard {
  id: string;
  lane: string;
  status: string;
  title: string;
  body: string;
  tag: string;
  position: number;
}

async function initTable(client: { query: (sql: string, params?: unknown[]) => Promise<{ rows: unknown[] }> }) {
  await client.query(`
    CREATE TABLE IF NOT EXISTS kanban_cards (
      id TEXT PRIMARY KEY,
      lane TEXT NOT NULL,
      status TEXT NOT NULL,
      title TEXT NOT NULL,
      body TEXT NOT NULL DEFAULT '',
      tag TEXT NOT NULL DEFAULT '',
      position INTEGER NOT NULL DEFAULT 0,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
}

export async function OPTIONS() {
  return new NextResponse(null, { status: 204, headers: CORS });
}

export async function GET() {
  const db = getPool();
  const client = await db.connect();
  try {
    await initTable(client);
    const result = await client.query<KanbanCard>(
      'SELECT id, lane, status, title, body, tag, position FROM kanban_cards ORDER BY lane, status, position, id'
    );
    const ts = await client.query<{ ts: string | null }>(
      'SELECT MAX(updated_at) AS ts FROM kanban_cards'
    );
    return NextResponse.json(
      { cards: result.rows, updatedAt: ts.rows[0]?.ts ?? null },
      { headers: CORS }
    );
  } finally {
    client.release();
  }
}

export async function POST(request: NextRequest) {
  let body: { password?: string; cards?: unknown[] };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Invalid JSON' }, { status: 400, headers: CORS });
  }

  if (body.password !== EDIT_PASSWORD) {
    return NextResponse.json({ error: 'Unauthorized' }, { status: 401, headers: CORS });
  }

  const cards = body.cards;
  if (!Array.isArray(cards)) {
    return NextResponse.json({ error: 'cards must be an array' }, { status: 400, headers: CORS });
  }

  const db = getPool();
  const client = await db.connect();
  try {
    await initTable(client);
    await client.query('BEGIN');
    await client.query('DELETE FROM kanban_cards');
    for (const card of cards as KanbanCard[]) {
      await client.query(
        `INSERT INTO kanban_cards (id, lane, status, title, body, tag, position)
         VALUES ($1, $2, $3, $4, $5, $6, $7)`,
        [
          String(card.id),
          String(card.lane),
          String(card.status),
          String(card.title),
          String(card.body ?? ''),
          String(card.tag ?? ''),
          Number(card.position ?? 0),
        ]
      );
    }
    await client.query('COMMIT');
    return NextResponse.json({ ok: true }, { headers: CORS });
  } catch (err) {
    await client.query('ROLLBACK');
    console.error('[kanban] POST failed:', err);
    return NextResponse.json({ error: 'Database error' }, { status: 500, headers: CORS });
  } finally {
    client.release();
  }
}
