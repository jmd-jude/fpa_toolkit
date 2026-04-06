import { Pool } from 'pg';

let pool: Pool | null = null;

function getPool(): Pool | null {
  const url = process.env.DATABASE_URL;
  if (!url) return null;
  if (!pool) pool = new Pool({ connectionString: url, ssl: false });
  return pool;
}

async function initTable(client: { query: (sql: string) => Promise<unknown> }) {
  await client.query(`
    CREATE TABLE IF NOT EXISTS usage_events (
      id SERIAL PRIMARY KEY,
      job_id TEXT NOT NULL,
      pipeline TEXT NOT NULL,
      file_name TEXT,
      files_processed INTEGER,
      pages_processed INTEGER,
      duration_seconds INTEGER,
      user_email TEXT,
      completed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
  `);
}

export interface UsageEvent {
  jobId: string;
  pipeline: 'document_index' | 'deposition_summary';
  fileName: string;
  filesProcessed?: number;
  pagesProcessed?: number;
  durationSeconds: number;
  userEmail?: string;
}

export async function recordUsage(event: UsageEvent): Promise<void> {
  await Promise.allSettled([
    insertToDb(event),
    sendDiscord(event),
  ]);
}

async function insertToDb(event: UsageEvent): Promise<void> {
  const db = getPool();
  if (!db) return;
  try {
    const client = await db.connect();
    try {
      await initTable(client);
      await client.query(
        `INSERT INTO usage_events
           (job_id, pipeline, file_name, files_processed, pages_processed, duration_seconds, user_email)
         VALUES ($1, $2, $3, $4, $5, $6, $7)`,
        [
          event.jobId,
          event.pipeline,
          event.fileName,
          event.filesProcessed ?? null,
          event.pagesProcessed ?? null,
          event.durationSeconds,
          event.userEmail ?? null,
        ]
      );
    } finally {
      client.release();
    }
  } catch (err) {
    console.error('[usage] DB insert failed:', err);
  }
}

async function sendDiscord(event: UsageEvent): Promise<void> {
  const url = process.env.DISCORD_WEBHOOK_URL;
  if (!url) return;

  const pipelineLabel = event.pipeline === 'document_index' ? 'Document Index' : 'Deposition Summary';
  const duration = formatDuration(event.durationSeconds);

  const lines = [
    `**Pipeline:** ${pipelineLabel}`,
    `**File:** ${event.fileName}`,
  ];
  if (event.pagesProcessed != null) lines.push(`**Pages processed:** ${event.pagesProcessed}`);
  if (event.filesProcessed != null) lines.push(`**Files processed:** ${event.filesProcessed}`);
  lines.push(`**Duration:** ${duration}`);
  if (event.userEmail) lines.push(`**User:** ${event.userEmail}`);

  try {
    await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: `**-- Job Complete --**\n${lines.join('\n')}` }),
    });
  } catch (err) {
    console.error('[usage] Discord notify failed:', err);
  }
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s}s`;
}
