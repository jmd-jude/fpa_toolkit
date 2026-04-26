import { NextRequest, NextResponse } from 'next/server';
import { getIronSession } from 'iron-session';
import { cookies } from 'next/headers';
import { sessionOptions, SessionData } from '@/lib/session';
import { getFreshToken, uploadToBox } from '@/lib/box';
import { createJob, updateJob, appendLog } from '@/lib/jobs';
import { recordUsage } from '@/lib/usage';
import { spawn } from 'child_process';
import { promises as fs } from 'fs';
import os from 'os';
import path from 'path';
import crypto from 'crypto';

export async function POST(request: NextRequest) {
  const session = await getIronSession<SessionData>(cookies(), sessionOptions);
  if (!session.accessToken) {
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401 });
  }

  const body = await request.json();
  const { folderId, folderName, enrich } = body as { folderId: string; folderName: string; enrich?: boolean };

  if (!folderId || !folderName) {
    return NextResponse.json({ error: 'folderId and folderName required' }, { status: 400 });
  }

  let accessToken: string;
  try {
    accessToken = await getFreshToken(session);
    await session.save();
  } catch {
    return NextResponse.json({ error: 'Token refresh failed — please re-authenticate' }, { status: 401 });
  }

  const jobId = crypto.randomUUID();
  createJob(jobId, folderId, folderName, 'document_index');

  // Fire and forget — runs in background while response is sent
  const userEmail = session.userEmail;
  runJob(jobId, accessToken, folderId, folderName, !!enrich, userEmail).catch((err) => {
    updateJob(jobId, { status: 'error', error: String(err) });
  });

  return NextResponse.json({ jobId });
}

async function runJob(
  jobId: string,
  accessToken: string,
  folderId: string,
  folderName: string,
  enrich: boolean,
  userEmail?: string
) {
  updateJob(jobId, { status: 'running', progress: 'Starting manifest generation...' });
  const startedAt = Date.now();

  const tmpDir = path.join(os.tmpdir(), `box-index-${jobId}`);
  await fs.mkdir(tmpDir, { recursive: true });

  try {
    const pythonDir = path.join(process.cwd(), 'python');
    const venvPython = path.join(process.cwd(), '.venv', 'bin', 'python3');
    const pythonBin = await fs.access(venvPython).then(() => venvPython).catch(() => 'python3');

    // Step 1 — manifest
    updateJob(jobId, { progress: 'Scanning Box folder...' });
    let filesProcessed = 0;
    await runPython(pythonBin, [
      path.join(pythonDir, 'manifest.py'),
      '--token', accessToken,
      '--folder-id', folderId,
      '--output-dir', tmpDir,
    ], (line) => {
      if (!line.trim()) return;
      appendLog(jobId, line.trim());
      const m = line.match(/Processing file (\d+)/i);
      if (m) {
        filesProcessed = parseInt(m[1], 10);
        updateJob(jobId, { progress: `Processing file ${m[1]}...` });
      }
    });

    // Find the manifest CSV
    const files = await fs.readdir(tmpDir);
    const manifestFile = files.find((f) => f.endsWith('_manifest.csv'));
    if (!manifestFile) throw new Error('manifest.py produced no output CSV');

    const slug = manifestFile.replace('_manifest.csv', '');
    const reportFile = path.join(tmpDir, `${slug}_report.xlsx`);

    // Step 2 (optional) — AI enrichment
    if (enrich) {
      updateJob(jobId, { progress: 'Running AI enrichment...' });
      await runPython(pythonBin, [
        path.join(pythonDir, 'enrich.py'),
        '--manifest-file', path.join(tmpDir, manifestFile),
        '--token', accessToken,
        '--model', process.env.BOX_AI_MODEL ?? 'google__gemini_2_5_pro',
      ], (line) => {
        if (!line.trim()) return;
        appendLog(jobId, line.trim());
        const m = line.match(/\[(\d+)\/(\d+)\]/);
        if (m) updateJob(jobId, { progress: `AI enrichment: ${m[1]} of ${m[2]} files...` });
      });
    }

    // Step 3 — persist to DB (non-fatal)
    const summaryFile = path.join(tmpDir, `${slug}_summary.csv`);
    try {
      await runPython(pythonBin, [
        path.join(pythonDir, 'db_persist.py'),
        '--job-id', jobId,
        '--manifest-file', path.join(tmpDir, manifestFile),
        '--summary-file', summaryFile,
      ], (line) => { if (line.trim()) appendLog(jobId, line.trim()); });
    } catch (err) {
      appendLog(jobId, `db_persist warning: ${String(err)}`);
    }

    // Step 5 — report
    updateJob(jobId, { progress: 'Generating Excel report...' });
    await runPython(pythonBin, [
      path.join(pythonDir, 'report.py'),
      '--input-file', path.join(tmpDir, manifestFile),
      '--output-file', reportFile,
    ], (line) => {
      if (line.trim()) appendLog(jobId, line.trim());
    });

    // Step 6 — upload to Box
    updateJob(jobId, { progress: 'Uploading report to Box...' });
    const dateStamp = new Date().toISOString().slice(0, 10);
    const uploadName = `${folderName}_index_${dateStamp}.xlsx`;
    const fileBuffer = await fs.readFile(reportFile);
    const boxFileUrl = await uploadToBox(accessToken, folderId, uploadName, fileBuffer);

    const durationSeconds = Math.round((Date.now() - startedAt) / 1000);
    updateJob(jobId, {
      status: 'complete',
      completedAt: new Date().toISOString(),
      boxFileUrl,
      progress: 'Done',
    });
    await recordUsage({
      jobId,
      pipeline: 'document_index',
      fileName: folderName,
      filesProcessed,
      durationSeconds,
      userEmail,
    });
  } finally {
    await fs.rm(tmpDir, { recursive: true, force: true });
  }
}

function runPython(
  python: string,
  args: string[],
  onLine?: (line: string) => void,
  extraEnv?: Record<string, string>
): Promise<void> {
  return new Promise((resolve, reject) => {
    const proc = spawn(python, args, extraEnv ? { env: { ...process.env, ...extraEnv } } : undefined);
    const outputLines: string[] = [];

    proc.stdout.on('data', (chunk: Buffer) => {
      const lines = chunk.toString().split('\n');
      lines.forEach((l) => { outputLines.push(l); onLine?.(l); });
    });
    proc.stderr.on('data', (chunk: Buffer) => {
      const lines = chunk.toString().split('\n');
      lines.forEach((l) => { outputLines.push(l); onLine?.(l); });
    });
    proc.on('close', (code) => {
      if (code === 0) {
        resolve();
      } else {
        // Surface the last meaningful line from Python output as the error
        const lastLine = outputLines.filter((l) => l.trim()).pop() ?? '';
        const msg = lastLine.replace(/^(ERROR:|error:)/i, '').trim() || `Python exited with code ${code}`;
        reject(new Error(msg));
      }
    });
    proc.on('error', reject);
  });
}
