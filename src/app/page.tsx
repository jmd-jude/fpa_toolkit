'use client';

import { useEffect, useRef, useState } from 'react';

interface AuthState {
  accessToken: string;
  userName: string;
  userEmail: string;
}

interface SelectedFolder {
  id: string;
  name: string;
}

interface JobStatus {
  status: 'queued' | 'running' | 'complete' | 'error';
  progress?: string;
  log?: string[];
  boxFileUrl?: string;
  error?: string;
}

declare global {
  interface Window {
    Box: {
      ContentPicker: new () => {
        show: (
          folderId: string,
          token: string,
          options: {
            container: string;
            type: string;
            maxSelectable: number;
            onChoose: (items: Array<{ id: string; name: string }>) => void;
            onCancel: () => void;
          }
        ) => void;
        hide: () => void;
      };
    };
  }
}

export default function Home() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [selectedFolder, setSelectedFolder] = useState<SelectedFolder | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [generating, setGenerating] = useState(false);
  const [pickerKey, setPickerKey] = useState(0);
  const [enrichAI, setEnrichAI] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);
  const pickerInstanceRef = useRef<ReturnType<typeof window.Box.ContentPicker.prototype.constructor> | null>(null);

  // Check auth on mount
  useEffect(() => {
    fetch('/api/auth/token')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.accessToken) {
          setAuth({
            accessToken: data.accessToken,
            userName: data.userName ?? '',
            userEmail: data.userEmail ?? '',
          });
        }
      })
      .catch(() => {})
      .finally(() => setAuthLoading(false));
  }, []);

  // Init Box Content Picker once authenticated
  useEffect(() => {
    if (!auth || !pickerRef.current) return;
    if (typeof window.Box === 'undefined') return;

    const picker = new window.Box.ContentPicker();
    pickerInstanceRef.current = picker;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (picker as any).show('0', auth.accessToken, {
      container: '#box-picker-container',
      type: 'folder',
      maxSelectable: 1,
      logoUrl: '/logo.png',
      onChoose: (items: { id: string; name: string }[]) => {
        const folder = items[0];
        setSelectedFolder({ id: folder.id, name: folder.name });
      },
      onCancel: () => {},
    });
  }, [auth, pickerKey]);

  // Poll job status — immediate first fetch, then every 1s
  useEffect(() => {
    if (!jobId) return;

    async function poll() {
      const res = await fetch(`/api/job/${jobId}`);
      if (!res.ok) return;
      const data: JobStatus = await res.json();
      setJob(data);
      if (data.status === 'complete' || data.status === 'error') {
        clearInterval(interval);
        setGenerating(false);
      }
    }

    poll();
    const interval = setInterval(poll, 1000);
    return () => clearInterval(interval);
  }, [jobId]);

  async function handleGenerate() {
    if (!selectedFolder) return;
    setGenerating(true);
    setJob({ status: 'queued', progress: 'Queuing job...' });

    const res = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folderId: selectedFolder.id, folderName: selectedFolder.name, enrich: enrichAI }),
    });

    if (!res.ok) {
      const err = await res.json();
      setJob({ status: 'error', error: err.error ?? 'Unknown error' });
      setGenerating(false);
      return;
    }

    const { jobId: id } = await res.json();
    setJobId(id);
  }

  async function handleLogout() {
    await fetch('/api/auth/logout', { method: 'POST' });
    setAuth(null);
    setSelectedFolder(null);
    setJobId(null);
    setJob(null);
  }

  function handleReset() {
    setSelectedFolder(null);
    setJobId(null);
    setJob(null);
    setGenerating(false);
    setPickerKey((k) => k + 1);
  }

  if (authLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="w-6 h-6 border-2 border-navy-700 border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="text-white px-8 py-4 flex items-center justify-between" style={{ backgroundColor: '#669966' }}>
        <div className="flex items-center gap-3">
          <img src="/logo.png" alt="FPAmed" className="h-8 w-auto brightness-0 invert" />
          <p className="text-white/80 text-sm hidden sm:block">Document Index Generator</p>
        </div>
        {auth && (
          <div className="flex items-center gap-4 text-sm">
            <span className="text-white/80">
              {auth.userName || auth.userEmail}
            </span>
            <button
              onClick={handleLogout}
              className="text-white/70 hover:text-white underline transition-colors"
            >
              Sign out
            </button>
          </div>
        )}
      </header>

      <main className={`flex-1 flex flex-col ${auth && !job ? '' : 'items-center justify-center px-8 py-12'}`}>
        {/* ── State 1: Unauthenticated ── */}
        {!auth && (
          <div className="text-center max-w-md">
            <div className="mb-8">
              <div className="w-16 h-16 bg-slate-200 rounded-2xl flex items-center justify-center mx-auto mb-4">
                <svg className="w-8 h-8 text-slate-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                    d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v9a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
                </svg>
              </div>
              <p className="text-slate-500 text-sm">
                Generate a formatted document index from any Box folder.
              </p>
            </div>
            <a
              href="/api/auth/login"
              className="inline-block text-white px-6 py-3 rounded-lg font-medium transition-colors"
              style={{ backgroundColor: '#669966' }}
              onMouseOver={e => (e.currentTarget.style.backgroundColor = '#4f7a4f')}
              onMouseOut={e => (e.currentTarget.style.backgroundColor = '#669966')}
            >
              Connect Box Account
            </a>
          </div>
        )}

        {/* ── State 2: Authenticated — folder selection ── */}
        {auth && !job && (
          <div className="w-full flex flex-col" style={{ height: 'calc(100vh - 73px)' }}>
            {/* Box Content Picker */}
            <div
              key={pickerKey}
              id="box-picker-container"
              ref={pickerRef}
              className="flex-1 overflow-hidden bg-white"
            />

            {selectedFolder && (
              <div className="mt-6 flex items-center justify-between text-white rounded-lg px-5 py-4" style={{ backgroundColor: '#669966' }}>
                <div className="flex items-center gap-3">
                  <div className="w-8 h-8 bg-white/10 rounded-full flex items-center justify-center flex-shrink-0">
                    <svg className="w-4 h-4 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                  </div>
                  <div>
                    <p className="text-xs text-white/70 uppercase tracking-wide font-medium mb-0.5">
                      Selected folder
                    </p>
                    <p className="font-semibold">{selectedFolder.name}</p>
                  </div>
                </div>
                <div className="flex items-center gap-4">
                  <label className="flex items-center gap-2 text-sm text-white/80 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={enrichAI}
                      onChange={(e) => setEnrichAI(e.target.checked)}
                      className="w-4 h-4 rounded accent-white cursor-pointer"
                    />
                    AI enrichment
                  </label>
                  <button
                    onClick={handleGenerate}
                    disabled={generating}
                    className="bg-white text-slate-900 px-6 py-2.5 rounded-lg font-medium hover:bg-slate-100 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  >
                    Generate Index
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── State 3: Job running / complete / error ── */}
        {auth && job && (
          <div className="w-full max-w-2xl text-center">
            {(job.status === 'queued' || job.status === 'running') && (
              <div>
                <div className="w-12 h-12 border-4 border-slate-200 rounded-full animate-spin mx-auto mb-6" style={{ borderTopColor: '#669966' }} />
                <h2 className="text-xl font-semibold text-slate-800 mb-2">Generating index…</h2>
                <p className="text-slate-500 text-sm mb-4">{job.progress ?? 'Working…'}</p>
                {job.log && job.log.length > 0 && (
                  <div className="text-left bg-slate-900 rounded-lg p-4 font-mono text-xs text-slate-300 overflow-hidden">
                    {job.log.map((line, i) => (
                      <div key={i} className={i === job.log!.length - 1 ? 'text-white' : 'text-slate-500'}>
                        {line}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {job.status === 'complete' && (
              <div>
                <div className="w-12 h-12 bg-green-100 rounded-full flex items-center justify-center mx-auto mb-6">
                  <svg className="w-6 h-6 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                </div>
                <h2 className="text-xl font-semibold text-slate-800 mb-2">Index generated</h2>
                <p className="text-slate-500 text-sm mb-6">
                  The Excel report has been saved to your Box folder.
                </p>
                <div className="flex flex-col sm:flex-row gap-3 justify-center">
                  {job.boxFileUrl && (
                    <a
                      href={job.boxFileUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-white px-6 py-2.5 rounded-lg font-medium transition-colors"
                      style={{ backgroundColor: '#669966' }}
                      onMouseOver={e => (e.currentTarget.style.backgroundColor = '#4f7a4f')}
                      onMouseOut={e => (e.currentTarget.style.backgroundColor = '#669966')}
                    >
                      Open in Box
                    </a>
                  )}
                  <button
                    onClick={handleReset}
                    className="border border-slate-300 text-slate-700 px-6 py-2.5 rounded-lg font-medium hover:bg-slate-100 transition-colors"
                  >
                    Generate another
                  </button>
                </div>
              </div>
            )}

            {job.status === 'error' && (
              <div>
                <div className="w-12 h-12 bg-red-100 rounded-full flex items-center justify-center mx-auto mb-6">
                  <svg className="w-6 h-6 text-red-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </div>
                <h2 className="text-xl font-semibold text-slate-800 mb-2">Something went wrong</h2>
                <p className="text-slate-500 text-sm mb-1">{job.error}</p>
                <button
                  onClick={handleReset}
                  className="mt-6 border border-slate-300 text-slate-700 px-6 py-2.5 rounded-lg font-medium hover:bg-slate-100 transition-colors"
                >
                  Try again
                </button>
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
