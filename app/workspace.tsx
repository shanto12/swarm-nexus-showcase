import {SHOWCASE, capturedMissionId} from '@/lib/showcase';
'use client';
/* oxlint-disable next/no-html-link-for-pages -- Shared with the standalone Netlify React build, which has no Next router. */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import {
  ArrowUpRight,
  ArrowRight,
  Network,
  Sparkles,
  SlidersHorizontal,
  Layers,
  ShieldCheck,
  Zap,
  Plus,
  LogOut,
  RefreshCw,
  Pause,
  Play,
  Square,
  FileText,
  Download,
  MessageSquare,
  Activity,
  Check,
  Clock,
  AlertCircle,
  X,
  Settings2,
  LoaderCircle,
  ChevronRight,
  Workflow,
} from 'lucide-react';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Slider } from '@/components/ui/slider';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogTitle,
  AlertDialogDescription,
} from '@/components/ui/alert-dialog';
import { Progress } from '@/components/ui/progress';
import { api, number, time } from '@/lib/client';
import type {
  Assessment,
  Detail,
  Health,
  Mission,
  ToolInventory,
  User,
} from '@/lib/types';

const terminal = new Set([
  'completed',
  'cancelled',
  'blocked',
  'failed',
  'budget_exhausted',
]);
const statusName = (s: string) => s.replaceAll('_', ' ');
function Status({ value }: { value: string }) {
  return (
    <span className={'status ' + value}>
      <span />
      {statusName(value)}
    </span>
  );
}
function Mode({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: 'auto' | 'manual') => void;
}) {
  return (
    <Tabs value={value} onValueChange={(v) => onChange(v as 'auto' | 'manual')}>
      <TabsList aria-label="Allocation mode">
        <TabsTrigger value="auto">
          <Sparkles /> Auto
        </TabsTrigger>
        <TabsTrigger value="manual">
          <SlidersHorizontal /> Manual
        </TabsTrigger>
      </TabsList>
    </Tabs>
  );
}
function ResourceFields({
  mode,
  setMode,
  agents,
  setAgents,
  tokens,
  setTokens,
}: {
  mode: 'auto' | 'manual';
  setMode: (v: 'auto' | 'manual') => void;
  agents: number;
  setAgents: (v: number) => void;
  tokens: number;
  setTokens: (v: number) => void;
}) {
  const fieldId = useId();
  return (
    <>
      <Mode value={mode} onChange={setMode} />
      <label className="resource-label" htmlFor={fieldId + '-agents'}>
        {mode === 'auto' ? 'Agent ceiling' : 'Concurrent agents'}
        <strong>{agents}</strong>
      </label>
      <Slider
        id={fieldId + '-agents'}
        aria-label={mode === 'auto' ? 'Agent ceiling' : 'Concurrent agents'}
        value={[agents]}
        onValueChange={(v) => setAgents(Array.isArray(v) ? v[0] : v)}
        min={1}
        max={20}
        step={1}
      />
      <div className="scale-ends">
        <span>1 agent</span>
        <span>20 agents</span>
      </div>
      <label
        className="resource-label token-label"
        htmlFor={fieldId + '-tokens'}
      >
        {mode === 'auto' ? 'Token ceiling' : 'Token allowance'}
        <strong>{number(tokens)}</strong>
      </label>
      <input
        id={fieldId + '-tokens'}
        aria-label={mode === 'auto' ? 'Token ceiling' : 'Token allowance'}
        type="number"
        min={1000}
        max={1000000}
        step={1000}
        value={tokens}
        onChange={(e) => setTokens(Number(e.target.value))}
      />
      <p className="field-help">
        {mode === 'auto'
          ? 'Starts with a smaller allocation and grows within this ceiling.'
          : 'A fixed allowance shared by planning, workers, and verification.'}
      </p>
    </>
  );
}

export default function Workspace() {
  const [mode, setMode] = useState<'auto' | 'manual'>('auto'),
    [agents, setAgents] = useState(8),
    [tokens, setTokens] = useState(500000),
    [prompt, setPrompt] = useState('');
  const [user, setUser] = useState<User | null>(null),
    [health, setHealth] = useState<Health | null>(null),
    [missions, setMissions] = useState<Mission[]>([]),
    [detail, setDetail] = useState<Detail | null>(null),
    [selected, setSelected] = useState('');
  const [ready, setReady] = useState(false),
    [composerOpen, setComposerOpen] = useState(true),
    [loginOpen, setLoginOpen] = useState(false),
    [toolsOpen, setToolsOpen] = useState(false),
    [resourcesOpen, setResourcesOpen] = useState(false),
    [cancelOpen, setCancelOpen] = useState(false);
  const [email, setEmail] = useState(''),
    [password, setPassword] = useState(''),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(''),
    [notice, setNotice] = useState(''),
    [assessmentResponse, setAssessment] = useState<
      (Assessment & { requestKey?: string }) | null
    >(null),
    [toolbox, setToolbox] = useState<ToolInventory | null>(null),
    [view, setView] = useState('result'),
    [steering, setSteering] = useState('');
  const [editMode, setEditMode] = useState<'auto' | 'manual'>('auto'),
    [editAgents, setEditAgents] = useState(8),
    [editTokens, setEditTokens] = useState(500000);
  const launchKey = useRef<{ body: string; key: string } | null>(null),
    generation = useRef(0),
    composer = useRef<HTMLTextAreaElement>(null);
  const userId = user?.id;
  const assessmentKey = JSON.stringify([userId, prompt, agents, tokens, mode]);
  const assessment =
    assessmentResponse?.requestKey === assessmentKey
      ? assessmentResponse
      : null;
  const select = useCallback((id: string) => {
    setSelected(id);
    setComposerOpen(!id);
    setDetail(null);
    setView('result');
    window.history.replaceState(
      null,
      '',
      id ? '?mission=' + encodeURIComponent(id) : window.location.pathname,
    );
  }, []);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const epoch = generation.current;
    const [h, s] = await Promise.all([
      api<Health>('/health', undefined, signal),
      api<{ user: User | null }>('/session', undefined, signal),
    ]);
    if (epoch !== generation.current) return;
    setHealth(h);
    setUser(s.user);
    setReady(true);
    if (s.user) {
      const [list, tools] = await Promise.all([
        api<{ tasks: Mission[] }>('/tasks', undefined, signal),
        api<ToolInventory>('/tools', undefined, signal),
      ]);
      if (epoch !== generation.current) return;
      setMissions(list.tasks);
      setToolbox(tools);
    } else {
      setMissions([]);
      setDetail(null);
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    // oxlint-disable-next-line react/react-compiler -- refresh changes state only after awaited network responses; this is an async-helper false positive.
    refresh(controller.signal)
      .then(() => {
        if (!controller.signal.aborted) {
          const missionId = new URLSearchParams(window.location.search).get('mission') || (SHOWCASE ? capturedMissionId : '');
          setSelected(missionId);
          setComposerOpen(!missionId);
        }
      })
      .catch((e) => {
        if (!controller.signal.aborted) {
          setError(e.message);
          setReady(true);
        }
      });
    return () => controller.abort();
  }, [refresh]);
  useEffect(() => {
    if (!userId) return;
    const controller = new AbortController();
    let alive = true;
    const poll = async () => {
      try {
        const list = await api<{ tasks: Mission[] }>(
          '/tasks',
          undefined,
          controller.signal,
        );
        if (alive) setMissions(list.tasks);
        if (selected) {
          const data = await api<Detail>(
            '/tasks/' + selected,
            undefined,
            controller.signal,
          );
          if (alive) setDetail(data);
        }
      } catch (e) {
        if (alive && !controller.signal.aborted) setError((e as Error).message);
      }
    };
    void poll();
    const timer = setInterval(poll, 3000);
    return () => {
      alive = false;
      controller.abort();
      clearInterval(timer);
    };
  }, [userId, selected]);
  useEffect(() => {
    if (
      !userId ||
      mode !== 'auto' ||
      !prompt.trim() ||
      tokens < 1000 ||
      tokens > 1000000
    )
      return;
    const abort = new AbortController();
    const timer = setTimeout(() => {
      api<Assessment>(
        '/assess',
        {
          prompt,
          agent_limit: agents,
          token_budget: tokens,
          allocation_mode: mode,
        },
        abort.signal,
      )
        .then((value) => setAssessment({ ...value, requestKey: assessmentKey }))
        .catch(() => {});
    }, 650);
    return () => {
      abort.abort();
      clearTimeout(timer);
    };
  }, [userId, prompt, agents, tokens, mode, assessmentKey]);
  const act = async (name: string, fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(name);
    setError('');
    setNotice('');
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };
  const launch = useCallback(async () => {
    if (!user) {
      setLoginOpen(true);
      return;
    }
    if (
      !prompt.trim() ||
      prompt.length > 16000 ||
      tokens < 1000 ||
      tokens > 1000000
    )
      throw new Error(
        'Add a mission and choose a token limit from 1,000 to 1,000,000.',
      );
    const body = {
      prompt: prompt.trim(),
      agent_limit: agents,
      token_budget: tokens,
      allocation_mode: mode,
      max_attempts: 3,
    };
    const fingerprint = JSON.stringify(body);
    if (launchKey.current?.body !== fingerprint)
      launchKey.current = { body: fingerprint, key: crypto.randomUUID() };
    const created = await api<Mission>(
      '/tasks',
      body,
      undefined,
      launchKey.current.key,
    );
    launchKey.current = null;
    setPrompt('');
    setAssessment(null);
    select(created.id);
    setMissions((old) => [created, ...old.filter((m) => m.id !== created.id)]);
    setNotice('Mission launched. Your team is planning in the cloud.');
    setTimeout(
      () =>
        document
          .getElementById('mission-detail')
          ?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
      200,
    );
  }, [user, prompt, agents, tokens, mode, select]);
  const control = async (action: string) => {
    if (!selected) return;
    await api('/tasks/' + selected + '/' + action, {});
    setDetail(await api<Detail>('/tasks/' + selected));
    setNotice(
      action === 'pause'
        ? 'Paused. In-flight work can finish; new assignments wait.'
        : action === 'resume'
          ? 'Mission resumed.'
          : 'Mission cancelled. Saved work is retained.',
    );
  };
  const openResources = () => {
    if (!detail) return;
    setEditMode(detail.task.resources.mode);
    setEditAgents(detail.task.resources.agent_ceiling);
    setEditTokens(detail.task.resources.token_ceiling);
    setResourcesOpen(true);
  };
  useEffect(() => {
    const context = (
      document as Document & {
        modelContext?: {
          registerTool: (
            tool: unknown,
            options: { signal: AbortSignal },
          ) => void;
        };
      }
    ).modelContext;
    if (!context?.registerTool) return;
    const life = new AbortController();
    try {
      context.registerTool(
        {
          name: 'stage_swarm_mission',
          title: 'Stage a swarm mission',
          description:
            'Fill the visible mission composer and resource controls. Does not start paid work.',
          inputSchema: {
            type: 'object',
            properties: {
              prompt: { type: 'string', minLength: 1, maxLength: 16000 },
              mode: { enum: ['auto', 'manual'] },
              agentCeiling: { type: 'integer', minimum: 1, maximum: 20 },
              tokenCeiling: {
                type: 'integer',
                minimum: 1000,
                maximum: 1000000,
              },
            },
            required: ['prompt'],
            additionalProperties: false,
          },
          annotations: { readOnlyHint: false, untrustedContentHint: false },
          execute: (input: unknown) => {
            const v = input as Record<string, unknown>;
            if (
              !v ||
              typeof v.prompt !== 'string' ||
              !v.prompt.trim() ||
              v.prompt.length > 16000 ||
              Object.keys(v).some(
                (k) =>
                  !['prompt', 'mode', 'agentCeiling', 'tokenCeiling'].includes(
                    k,
                  ),
              )
            )
              throw new Error('Invalid mission');
            if (
              v.mode !== undefined &&
              v.mode !== 'auto' &&
              v.mode !== 'manual'
            )
              throw new Error('Invalid mode');
            if (
              v.agentCeiling !== undefined &&
              (!Number.isInteger(v.agentCeiling) ||
                Number(v.agentCeiling) < 1 ||
                Number(v.agentCeiling) > 20)
            )
              throw new Error('Invalid agent ceiling');
            if (
              v.tokenCeiling !== undefined &&
              (!Number.isInteger(v.tokenCeiling) ||
                Number(v.tokenCeiling) < 1000 ||
                Number(v.tokenCeiling) > 1000000)
            )
              throw new Error('Invalid token ceiling');
            setPrompt(v.prompt);
            if (v.mode) setMode(v.mode as 'auto' | 'manual');
            if (v.agentCeiling) setAgents(Number(v.agentCeiling));
            if (v.tokenCeiling) setTokens(Number(v.tokenCeiling));
            setComposerOpen(true);
            requestAnimationFrame(() => composer.current?.focus());
            return { staged: true, launched: false };
          },
        },
        { signal: life.signal },
      );
    } catch {}
    return () => life.abort();
  }, []);
  const running = missions.filter((m) =>
      ['running', 'queued'].includes(m.status),
    ).length,
    completed = missions.filter((m) => m.status === 'completed').length;
  return (
    <div className={"nexus-shell" + (SHOWCASE ? " public-showcase" : "")}>
      <aside className="command-rail" aria-label="Workspace navigation">
        <a href="/" className="rail-mark" aria-label="Swarm Nexus home">
          <Network size={26} />
        </a>
        <button
          className="rail-action"
          aria-label="Compose a mission"
          title="Compose a mission"
          onClick={() => {
            setComposerOpen(true);
            requestAnimationFrame(() => composer.current?.focus());
          }}
        >
          <Plus size={21} />
        </button>
        <a
          href="#mission-history"
          className="rail-action"
          aria-label="Browse missions"
          title="Browse missions"
        >
          <Layers size={20} />
        </a>
        <button
          className="rail-action"
          aria-label="Open tools and observability"
          title="Tools and observability"
          onClick={() => (user ? setToolsOpen(true) : setLoginOpen(true))}
        >
          <Activity size={20} />
        </button>
        <div className="rail-bottom">
          <span>N</span>
          <span className="rail-caption">NEXUS</span>
        </div>
      </aside>
      <header className="mast">
        <a className="brand" href="/">
          <Network size={28} />
          <span>
            SWARM<span className="brand-light"> / NEXUS</span>
          </span>
        </a>
        <div className="mast-right">
          <span
            className={
              'connection ' + (health?.status === 'ok' ? 'connected' : '')
            }
          >
            <span />
            {health?.status === 'ok'
              ? (SHOWCASE ? 'Captured execution' : 'Cloud connected')
              : ready
                ? 'Runtime unavailable'
                : 'Connecting'}
          </span>
          <a
            className="text-link original-link"
            href="https://swarm-nexus-shanto.netlify.app/"
            target="_blank"
            rel="noreferrer"
          >
            Authenticated workspace <ArrowUpRight size={15} />
          </a>
          {SHOWCASE ? (<a className="text-link" href="https://github.com/shanto12/swarm-nexus-showcase" target="_blank" rel="noreferrer">Source code <ArrowUpRight size={15} /></a>) : user ? (
            <button
              className="icon-button"
              title="Sign out"
              aria-label="Sign out"
              onClick={() =>
                void act('logout', async () => {
                  await api('/logout', {});
                  generation.current++;
                  setUser(null);
                  setMissions([]);
                  setDetail(null);
                  setPassword('');
                  select('');
                  setNotice('Signed out. Cloud missions continue.');
                })
              }
            >
              <LogOut size={18} />
            </button>
          ) : (
            <button className="secondary" onClick={() => setLoginOpen(true)}>
              Sign in <ArrowRight size={15} />
            </button>
          )}
        </div>
      </header>
      <main className="workspace">
        {SHOWCASE && <section className="showcase-note" aria-label="About this public walkthrough"><div><strong>REAL CLOUD EXECUTION · PUBLIC WALKTHROUGH</strong><p>Inspect a completed mission: two independent workers, synthesis, verification and downloadable evidence. Captured September 14, 2026. This view is read-only; live execution stays behind owner authentication.</p></div><a href="https://github.com/shanto12/swarm-nexus-showcase#architecture" target="_blank" rel="noreferrer">Explore the architecture <ArrowUpRight size={16}/></a></section>}
        <div className="page-heading">
          <div>
            <div className="eyebrow">
              <span className="signal" /> WORKSPACE / MISSION CONTROL
            </div>
            <h1>
              One mission. <span className="dim">Collective intelligence.</span>
            </h1>
          </div>
          <div className="heading-detail">
            <span className="edition">CLOUD AGENT RUNTIME</span>
            <span>DeepSeek V4 Flash</span>
            <button
              className="text-link"
              onClick={() => (user ? setToolsOpen(true) : setLoginOpen(true))}
            >
              Tools & observability <ArrowUpRight size={15} />
            </button>
          </div>
        </div>
        {error && (
          <div className="banner error" role="alert">
            <AlertCircle size={18} />
            <span>{error}</span>
            <button
              className="icon-button"
              aria-label="Dismiss error"
              onClick={() => setError('')}
            >
              <X size={17} />
            </button>
          </div>
        )}
        {notice && (
          <output className="banner success">
            <Check size={18} />
            <span>{notice}</span>
            <button
              className="icon-button"
              aria-label="Dismiss notification"
              onClick={() => setNotice('')}
            >
              <X size={17} />
            </button>
          </output>
        )}
        <section
          className="command-overview"
          aria-label="Workspace overview"
          hidden={!!selected && !SHOWCASE}
        >
          <div className="orbit-banner">
            {/* oxlint-disable-next-line next/no-img-element -- Shared with the standalone Netlify client; explicit dimensions prevent layout shift without a Next image server. */}
            <img src="/swarm-orbit.png" alt="" width={1672} height={941} />
            <div className="orbit-copy">
              <span className="eyebrow">INTELLIGENCE, ORCHESTRATED</span>
              <h2>
                From a single brief
                <br />
                to a coordinated team.
              </h2>
              <p>Plan. Delegate. Verify. Every step, in view.</p>
              <a href="#new-mission" className="text-link">
                Compose your next mission <ArrowRight size={16} />
              </a>
            </div>
            <span className="orbit-caption">SWARM STUDY / 01</span>
          </div>
          <div className="overview-metrics">
            <div>
              <span>
                <Activity size={15} /> Active missions
              </span>
              <strong>{user ? String(running).padStart(2, '0') : '—'}</strong>
              <small>
                {user
                  ? 'Running or queued in the cloud'
                  : 'Sign in to view your workspace'}
              </small>
            </div>
            <div>
              <span>
                <ShieldCheck size={15} /> Verified outcomes
              </span>
              <strong>{user ? String(completed).padStart(2, '0') : '—'}</strong>
              <small>Completed missions in your history</small>
            </div>
            <div>
              <span>
                <Network size={15} /> Execution model
              </span>
              <strong className="metric-model">
                DeepSeek <em>V4 Flash</em>
              </strong>
              <small>Adaptive teams · Persistent progress</small>
            </div>
          </div>
        </section>
        <div className="launch-layout" hidden={!composerOpen}>
          <section className="composer card" id="new-mission">
            <div className="section-top">
              <div>
                <span className="eyebrow">01 / MISSION BRIEF</span>
                <h2>What should the swarm solve?</h2>
              </div>
              <Sparkles size={20} />
            </div>
            <textarea
              ref={composer}
              aria-label="Your mission"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Describe the outcome. Add your context, constraints, and what great looks like…"
              maxLength={16000}
            />
            <div className="composer-foot">
              <span>{prompt.length.toLocaleString()} / 16,000</span>
              <span>Progress stays in the cloud</span>
            </div>
            {assessment && (
              <div className="assessment">
                <span className="assessment-icon">
                  <Workflow size={20} />
                </span>
                <div>
                  <strong>{assessment.complexity} scope</strong>
                  <p>
                    Starts with{' '}
                    {number(assessment.initial_token_allocation || 0)} tokens ·
                    up to {assessment.recommended_agents} agents recommended
                  </p>
                  <span>Reassessed after planning</span>
                </div>
              </div>
            )}
            <div className="compose-controls">
              <div className="mode-summary">
                {mode === 'auto' ? (
                  <Sparkles size={17} />
                ) : (
                  <SlidersHorizontal size={17} />
                )}
                <span>
                  {mode === 'auto'
                    ? 'Adaptive allocation'
                    : 'Manual allocation'}
                </span>
              </div>
              <button
                className="primary"
                disabled={
                  !prompt.trim() || !!busy || tokens < 1000 || tokens > 1000000
                }
                onClick={() => void act('launch', launch)}
              >
                {busy === 'launch' ? (
                  <LoaderCircle className="spin" size={17} />
                ) : null}
                {!user ? 'Sign in to launch' : 'Launch mission'}
                <ArrowRight size={18} />
              </button>
            </div>
            <div className="prompt-starters">
              <span>Try a starting point</span>
              <button
                onClick={() => {
                  setPrompt(
                    'Research and compare three practical approaches to this problem: [describe problem]. Create an evidence-backed recommendation with trade-offs and a short implementation plan.',
                  );
                  composer.current?.focus();
                }}
              >
                Research & compare <ArrowUpRight size={13} />
              </button>
              <button
                onClick={() => {
                  setPrompt(
                    'Create a detailed plan for [describe outcome]. Split independent research, identify dependencies, and verify the final recommendation against my requirements.',
                  );
                  composer.current?.focus();
                }}
              >
                Plan a project <ArrowUpRight size={13} />
              </button>
            </div>
          </section>
          <aside className="resource-card card">
            <div className="eyebrow">02 / RESOURCE POLICY</div>
            <h2>
              {mode === 'auto'
                ? 'Scale with the task.'
                : 'Your team. Your limits.'}
            </h2>
            <p>
              {mode === 'auto'
                ? 'Agents scale with ready work. The team expands its token allocation as the task develops.'
                : 'Choose a fixed team capacity and token allowance.'}
            </p>
            <ResourceFields
              mode={mode}
              setMode={setMode}
              agents={agents}
              setAgents={setAgents}
              tokens={tokens}
              setTokens={setTokens}
            />
            <div className="resource-note">
              <ShieldCheck size={17} />
              <span>
                {toolbox
                  ? `Workspace capacity: ${toolbox.capacity.max_global_agents} simultaneous agents.`
                  : 'Your limits apply to the whole mission.'}
              </span>
            </div>
          </aside>
        </div>
        <div className="workspace-divider" id="mission-history">
          <h2>
            Mission library <span className="count">{missions.length}</span>
          </h2>
          <div className="mission-toolbar">
            {selected && !SHOWCASE && (
              <button
                className="secondary"
                aria-expanded={composerOpen}
                onClick={() => {
                  setComposerOpen(!composerOpen);
                  if (!composerOpen)
                    requestAnimationFrame(() => composer.current?.focus());
                }}
              >
                <Plus size={15} />
                {composerOpen ? 'Hide composer' : 'New mission'}
              </button>
            )}
            <span className="quiet">
              {running} active · {completed} verified
            </span>
            <button
              className="icon-button"
              aria-label="Refresh workspace"
              disabled={!!busy}
              onClick={() => void act('refresh', () => refresh())}
            >
              <RefreshCw size={17} />
            </button>
          </div>
        </div>
        {!user ? (
          <section className="empty-missions">
            <div className="empty-symbol">
              <ShieldCheck size={28} />
            </div>
            <div>
              <h3>Your private command center.</h3>
              <p>Sign in to see your missions and launch a cloud team.</p>
            </div>
            <button className="secondary" onClick={() => setLoginOpen(true)}>
              Sign in <ArrowRight size={16} />
            </button>
          </section>
        ) : !missions.length ? (
          <section className="empty-missions">
            <div className="empty-symbol">
              <Layers size={28} />
            </div>
            <div>
              <h3>A clear place for your next big task.</h3>
              <p>
                Your missions, agent activity, and deliverables will live here.
              </p>
            </div>
            <Plus size={24} />
          </section>
        ) : (
          <div className="mission-grid">
            <div className="mission-list" aria-label="Mission history">
              {missions.map((m) => (
                <button
                  key={m.id}
                  className={
                    'mission-row ' + (selected === m.id ? 'selected' : '')
                  }
                  onClick={() => select(m.id)}
                >
                  <div>
                    <Status value={m.status} />
                    <span className="mission-mode">
                      {m.resources.mode === 'auto' ? (
                        <Sparkles size={13} />
                      ) : (
                        <SlidersHorizontal size={13} />
                      )}
                    </span>
                  </div>
                  <h3>{m.prompt}</h3>
                  <div className="mission-meta">
                    <span>
                      <Network size={13} />
                      {m.active_agents} / {m.agent_limit}
                    </span>
                    <span>{number(m.tokens_used)} tokens</span>
                  </div>
                  <div className="mission-date">
                    {time(m.created_at)} CT <ChevronRight size={14} />
                  </div>
                </button>
              ))}
            </div>
            <section id="mission-detail" className="mission-detail card">
              {!selected ? (
                <div className="detail-empty">
                  <Layers size={32} />
                  <h2>Select a mission</h2>
                  <p>See its result, team, resource decisions, and files.</p>
                </div>
              ) : !detail ? (
                <output className="detail-empty">
                  <LoaderCircle className="spin" size={28} />
                  <p>Loading saved mission…</p>
                </output>
              ) : (
                <>
                  <div className="detail-header">
                    <div className="section-top">
                      <div className="eyebrow">
                        MISSION / {detail.task.id.slice(0, 8).toUpperCase()}
                      </div>
                      <Status value={detail.task.status} />
                    </div>
                    <h2>
                      {detail.task.prompt.length > 160
                        ? detail.task.prompt.slice(0, 157) + '…'
                        : detail.task.prompt}
                    </h2>
                    {detail.task.prompt.length > 160 && (
                      <details className="mission-brief">
                        <summary>Read full mission brief</summary>
                        <p>{detail.task.prompt}</p>
                      </details>
                    )}
                    <div className="detail-actions">
                      <span>
                        <Clock size={14} />
                        {time(detail.task.created_at)} CT
                      </span>
                      {!terminal.has(detail.task.status) && (
                        <div>
                          <button
                            className="secondary"
                            disabled={!!busy}
                            onClick={() =>
                              void act('control', () =>
                                control(
                                  detail.task.status === 'paused'
                                    ? 'resume'
                                    : 'pause',
                                ),
                              )
                            }
                          >
                            {detail.task.status === 'paused' ? (
                              <Play size={14} />
                            ) : (
                              <Pause size={14} />
                            )}{' '}
                            {detail.task.status === 'paused'
                              ? 'Resume'
                              : 'Pause'}
                          </button>
                          <button
                            className="icon-button"
                            aria-label="Cancel mission"
                            disabled={!!busy}
                            onClick={() => setCancelOpen(true)}
                          >
                            <Square size={15} />
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                  <div className="resource-strip">
                    <div>
                      <span>Team now</span>
                      <strong>
                        {detail.task.active_agents}
                        <small> / {detail.task.agent_limit}</small>
                      </strong>
                    </div>
                    <div>
                      <span>Allocated</span>
                      <strong>
                        {number(detail.task.token_budget)}
                        <small> tokens</small>
                      </strong>
                    </div>
                    <div>
                      <span>Used</span>
                      <strong>{number(detail.task.tokens_used)}</strong>
                    </div>
                    <div>
                      <span>Est. spend</span>
                      <strong>
                        ${detail.task.estimated_cost_usd.toFixed(4)}
                      </strong>
                    </div>
                    <button
                      className="icon-button"
                      aria-label="Configure mission resources"
                      title="Configure resources"
                      disabled={terminal.has(detail.task.status)}
                      onClick={openResources}
                    >
                      <Settings2 size={17} />
                    </button>
                  </div>
                  <Tabs
                    value={view}
                    onValueChange={(v) => setView(String(v))}
                    className="detail-tabs"
                  >
                    <TabsList aria-label="Mission views" variant="line">
                      <TabsTrigger value="result">
                        <FileText /> Result
                      </TabsTrigger>
                      <TabsTrigger value="swarm">
                        <Network /> Swarm
                      </TabsTrigger>
                      <TabsTrigger value="resources">
                        <SlidersHorizontal /> Resources
                      </TabsTrigger>
                      <TabsTrigger value="activity">
                        <Activity /> Activity
                      </TabsTrigger>
                      <TabsTrigger value="files">
                        <Layers /> Files <span>{detail.artifacts.length}</span>
                      </TabsTrigger>
                    </TabsList>
                    <TabsContent value="result">
                      <div className="outcome">
                        <div className="outcome-progress">
                          <span>
                            {
                              detail.items.filter(
                                (i) => i.status === 'completed',
                              ).length
                            }{' '}
                            / {detail.items.length} assignments complete
                          </span>
                          <ShieldCheck size={17} />
                        </div>
                        <Progress
                          aria-label="Assignment completion"
                          value={
                            detail.items.length
                              ? (detail.items.filter(
                                  (i) => i.status === 'completed',
                                ).length /
                                  detail.items.length) *
                                100
                              : 0
                          }
                        />
                        {detail.task.result ? (
                          <div className="result-content">
                            {SHOWCASE ? detail.task.result.split('\n').map((line, i) => line.startsWith('# ') ? <h3 key={i}>{line.slice(2)}</h3> : line.startsWith('## ') ? <h4 key={i}>{line.slice(3)}</h4> : line.startsWith('### ') ? <h4 key={i}>{line.slice(4)}</h4> : <p key={i}>{line || '\u00a0'}</p>) : detail.task.result}
                          </div>
                        ) : (
                          <div className="pending-outcome">
                            <Workflow size={30} />
                            <h3>
                              {detail.task.status === 'cancelled'
                                ? 'Mission cancelled'
                                : detail.task.blocker
                                  ? 'Your team needs attention'
                                  : 'Your team is shaping the result.'}
                            </h3>
                            <p>
                              {detail.task.blocker ||
                                'Open Swarm to see the plan, current assignments, and collaboration.'}
                            </p>
                            <button
                              className="text-link"
                              onClick={() => setView('swarm')}
                            >
                              Follow the swarm <ArrowRight size={16} />
                            </button>
                          </div>
                        )}
                        {detail.task.status === 'budget_exhausted' && (
                          <button
                            className="secondary"
                            disabled={
                              !!busy || detail.task.token_budget >= 1000000
                            }
                            onClick={() => {
                              setEditTokens(
                                Math.min(
                                  1000000,
                                  Math.max(
                                    detail.task.token_budget + 100000,
                                    detail.task.resources.token_ceiling +
                                      100000,
                                  ),
                                ),
                              );
                              setResourcesOpen(true);
                            }}
                          >
                            Increase budget & continue <ArrowRight size={16} />
                          </button>
                        )}
                      </div>
                    </TabsContent>
                    <TabsContent value="swarm">
                      <div className="swarm-heading">
                        <div>
                          <h3>The plan, in motion.</h3>
                          <p>
                            {detail.items.length} assignments ·{' '}
                            {detail.handoffs.length} saved handoffs
                          </p>
                        </div>
                        <span className="auto-pill">
                          {detail.task.resources.mode === 'auto'
                            ? 'Adaptive'
                            : 'Manual'}{' '}
                          team
                        </span>
                      </div>
                      <div className="assignment-flow">
                        {detail.items.map((item, index) => (
                          <article
                            className={'assignment ' + item.status}
                            id={'assignment-' + item.id}
                            key={item.id}
                          >
                            <div className="assignment-index">
                              {item.status === 'completed' ? (
                                <Check size={16} />
                              ) : item.status === 'running' ? (
                                <LoaderCircle size={16} className="spin" />
                              ) : (
                                String(index + 1).padStart(2, '0')
                              )}
                            </div>
                            <div className="assignment-main">
                              <div className="assignment-top">
                                <span>
                                  {item.role === 'planner'
                                    ? 'Coordinator'
                                    : item.role === 'verifier'
                                      ? 'Verifier'
                                      : 'Worker'}
                                </span>
                                <Status value={item.status} />
                              </div>
                              <h4>{item.title}</h4>
                              {item.dependencies.length > 0 && (
                                <div className="depends">
                                  After{' '}
                                  {item.dependencies.map((id, k) => (
                                    <a href={'#assignment-' + id} key={id}>
                                      {k > 0 ? ', ' : ''}#
                                      {detail.items.findIndex(
                                        (i) => i.id === id,
                                      ) + 1}
                                    </a>
                                  ))}
                                </div>
                              )}
                              <span className="assignment-agent">
                                {item.assigned_agent ||
                                  'Waiting for assignment'}
                                {item.attempt
                                  ? ' · attempt ' + item.attempt
                                  : ''}
                              </span>
                              {item.output && (
                                <details>
                                  <summary>Read saved output</summary>
                                  <pre>{item.output}</pre>
                                </details>
                              )}
                              {item.error && (
                                <p className="inline-error">{item.error}</p>
                              )}
                            </div>
                          </article>
                        ))}
                      </div>
                      <h3 className="handoff-title">
                        <MessageSquare size={18} /> Agent handoffs
                      </h3>
                      {!detail.handoffs.length ? (
                        <p className="quiet">
                          No handoffs have been committed yet.
                        </p>
                      ) : (
                        detail.handoffs.map((h) => (
                          <div className="handoff" key={h.id}>
                            <div>
                              <span>
                                {detail.items.find(
                                  (i) => i.id === h.source_item_id,
                                )?.title || 'Coordinator'}
                              </span>
                              <ArrowRight size={14} />
                              <span>
                                {detail.items.find(
                                  (i) => i.id === h.target_item_id,
                                )?.title || 'Team'}
                              </span>
                            </div>
                            <p>{h.message}</p>
                            <time>{time(h.created_at)} CT</time>
                          </div>
                        ))
                      )}
                    </TabsContent>
                    <TabsContent value="resources">
                      <div className="resource-overview">
                        <div className="section-top">
                          <h3>
                            {detail.task.resources.mode === 'auto'
                              ? 'Adaptive resource decisions'
                              : 'Manual resource settings'}
                          </h3>
                          <Sparkles size={18} />
                        </div>
                        <div className="resource-figures">
                          <div>
                            <span>Agent ceiling</span>
                            <strong>
                              {detail.task.resources.agent_ceiling}
                            </strong>
                          </div>
                          <div>
                            <span>Token ceiling</span>
                            <strong>
                              {number(detail.task.resources.token_ceiling)}
                            </strong>
                          </div>
                          <div>
                            <span>Reserved now</span>
                            <strong>
                              {number(detail.task.tokens_reserved)}
                            </strong>
                          </div>
                          <div>
                            <span>Unconfirmed usage</span>
                            <strong>
                              {number(detail.task.tokens_uncertain)}
                            </strong>
                          </div>
                        </div>
                        <p className="quiet">
                          {detail.task.resources.assessment.basis ===
                          'saved_plan'
                            ? 'Based on the saved dependency plan.'
                            : 'Initial estimate from the mission brief.'}{' '}
                          {detail.task.resources.assessment.reasons?.join(
                            ' · ',
                          )}
                        </p>
                        <div className="allocation-bar">
                          <div
                            style={{
                              width:
                                Math.min(
                                  100,
                                  (detail.task.token_budget /
                                    detail.task.resources.token_ceiling) *
                                    100,
                                ) + '%',
                            }}
                          />
                        </div>
                        <p className="field-help">
                          {number(detail.task.token_budget)} allocated of{' '}
                          {number(detail.task.resources.token_ceiling)} allowed.
                          Allocation is permission to use tokens, not actual
                          consumption.
                        </p>
                      </div>
                      <div className="events resource-events">
                        {detail.events
                          .filter(
                            (e) =>
                              e.kind.startsWith('resource') ||
                              e.kind === 'budget_increased',
                          )
                          .map((e) => (
                            <article key={e.id}>
                              <div className="event-dot" />
                              <div>
                                <strong>
                                  {statusName(e.kind).replace('resource ', '')}
                                </strong>
                                <p>{e.message}</p>
                                <time>{time(e.created_at)} CT</time>
                              </div>
                            </article>
                          ))}
                      </div>
                    </TabsContent>
                    <TabsContent value="activity">
                      <div className="section-top">
                        <h3>Activity ledger</h3>
                        <span className="quiet">Central Time</span>
                      </div>
                      <div className="events">
                        {[...detail.events].reverse().map((e) => (
                          <article key={e.id}>
                            <div className={'event-dot ' + e.kind} />
                            <div>
                              <div className="event-heading">
                                <strong>{statusName(e.kind)}</strong>
                                <span>{e.agent}</span>
                              </div>
                              <p>{e.message}</p>
                              <time>{time(e.created_at)} CT</time>
                            </div>
                          </article>
                        ))}
                      </div>
                    </TabsContent>
                    <TabsContent value="files">
                      <div className="section-top">
                        <h3>Mission deliverables</h3>
                        <span className="quiet">
                          {detail.artifacts.length} saved
                        </span>
                      </div>
                      {!detail.artifacts.length ? (
                        <div className="pending-outcome">
                          <FileText size={28} />
                          <p>Your team’s saved files will appear here.</p>
                        </div>
                      ) : (
                        <div className="files">
                          {detail.artifacts.map((f) => (
                            <a
                              key={f.id}
                              href={
                                SHOWCASE ? '/capture/artifacts/' + encodeURIComponent(f.name) : '/api/tasks/' + detail.task.id + '/artifacts/' + f.id
                              }
                              download
                            >
                              <div className="file-icon">
                                <FileText size={21} />
                              </div>
                              <div>
                                <strong>{f.name}</strong>
                                <span>
                                  {number(f.size)} B · {time(f.created_at)} CT
                                </span>
                              </div>
                              <Download size={18} />
                            </a>
                          ))}
                        </div>
                      )}
                    </TabsContent>
                  </Tabs>
                  {!terminal.has(detail.task.status) && (
                    <form
                      className="steering"
                      onSubmit={(e) => {
                        e.preventDefault();
                        void act('steer', async () => {
                          await api('/tasks/' + selected + '/steer', {
                            message: steering,
                          });
                          setSteering('');
                          setDetail(await api<Detail>('/tasks/' + selected));
                          setNotice(
                            'Direction saved for upcoming assignments.',
                          );
                        });
                      }}
                    >
                      <label htmlFor="steering">
                        Add direction to the team
                      </label>
                      <div>
                        <input
                          id="steering"
                          value={steering}
                          onChange={(e) => setSteering(e.target.value)}
                          placeholder="A constraint, clarification, or change in direction…"
                          maxLength={10000}
                        />
                        <button
                          aria-label="Send direction"
                          className="primary"
                          disabled={!steering.trim() || !!busy}
                        >
                          <ArrowRight size={18} />
                        </button>
                      </div>
                    </form>
                  )}
                </>
              )}
            </section>
          </div>
        )}
        <footer>
          <span>
            <Zap size={14} /> {SHOWCASE ? "Captured mission · Actual saved artifacts" : "Cloud execution · Progress saved"}
          </span>
          <span>
            Adaptive teams · Shared findings · Independent verification
          </span>
        </footer>
      </main>
      <Dialog open={loginOpen} onOpenChange={setLoginOpen}>
        <DialogContent className="nexus-dialog">
          <Network className="dialog-symbol" size={30} />
          <DialogTitle>Enter your workspace.</DialogTitle>
          <DialogDescription>
            Use your existing Swarm Studio owner credentials. Nexus keeps a
            separate mission history.
          </DialogDescription>
          <form
            className="login-form"
            onSubmit={(e) => {
              e.preventDefault();
              void act('login', async () => {
                const data = await api<{ user: User }>('/login', {
                  email,
                  password,
                });
                generation.current++;
                setUser(data.user);
                setPassword('');
                setLoginOpen(false);
                await refresh();
                setNotice('Signed in. Your mission is ready to launch.');
              });
            }}
          >
            <label htmlFor="email">Email address</label>
            <input
              id="email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
            />
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            {error && (
              <p className="inline-error" role="alert">
                {error}
              </p>
            )}
            <button className="primary" disabled={!!busy}>
              {busy === 'login' ? 'Signing in…' : 'Sign in'}
              <ArrowRight size={17} />
            </button>
          </form>
        </DialogContent>
      </Dialog>
      <Dialog open={toolsOpen} onOpenChange={setToolsOpen}>
        <DialogContent className="nexus-dialog wide-dialog">
          <DialogTitle>Tools & observability</DialogTitle>
          <DialogDescription>
            The capabilities available to this cloud team.
          </DialogDescription>
          <div className="observability-card">
            <div>
              <Activity size={20} />
              <strong>LangSmith</strong>
              <span
                className={
                  'auto-pill ' +
                  (!health?.observability.configured ? 'muted' : '')
                }
              >
                {health?.observability.configured
                  ? 'Configured'
                  : 'Not connected'}
              </span>
            </div>
            <p>
              {health?.observability.configured
                ? 'Assignment and DeepSeek model traces are enabled.'
                : 'A LangSmith API key is still needed to send traces. The activity ledger remains available in Nexus.'}
            </p>
            <span>
              {health?.observability.project || 'swarm-nexus-production'}
            </span>
            <a
              className="text-link"
              href="https://smith.langchain.com/"
              target="_blank"
              rel="noreferrer"
            >
              Open LangSmith <ArrowUpRight size={14} />
            </a>
          </div>
          <div className="tool-list">
            {toolbox?.tools.map((t) => (
              <div key={t.name}>
                <Check size={17} />
                <div>
                  <strong>{t.name.replaceAll('_', ' ')}</strong>
                  <p>{t.description}</p>
                </div>
              </div>
            ))}
          </div>
          <p className="field-help">{toolbox?.limits.execution}</p>
        </DialogContent>
      </Dialog>
      <Dialog open={resourcesOpen} onOpenChange={setResourcesOpen}>
        <DialogContent className="nexus-dialog">
          <DialogTitle>
            {detail?.task.status === 'budget_exhausted'
              ? 'Continue the mission'
              : 'Adjust mission resources'}
          </DialogTitle>
          <DialogDescription>
            Change the limits while preserving completed work and saved
            progress.
          </DialogDescription>
          {detail?.task.status === 'budget_exhausted' ? (
            <>
              <label htmlFor="continue-tokens">New token ceiling</label>
              <input
                id="continue-tokens"
                type="number"
                min={(detail?.task.token_budget || 0) + 1000}
                max={1000000}
                step={1000}
                value={editTokens}
                onChange={(e) => setEditTokens(Number(e.target.value))}
              />
            </>
          ) : (
            <ResourceFields
              mode={editMode}
              setMode={setEditMode}
              agents={editAgents}
              setAgents={setEditAgents}
              tokens={editTokens}
              setTokens={setEditTokens}
            />
          )}
          <p className="field-help">
            A lower agent ceiling takes effect as in-flight assignments finish.
          </p>
          {error && <p className="inline-error">{error}</p>}
          <button
            className="primary"
            disabled={!!busy || editTokens < 1000 || editTokens > 1000000}
            onClick={() =>
              void act('resources', async () => {
                await api(
                  '/tasks/' +
                    selected +
                    (detail?.task.status === 'budget_exhausted'
                      ? '/budget'
                      : '/resources'),
                  detail?.task.status === 'budget_exhausted'
                    ? { token_budget: editTokens }
                    : {
                        mode: editMode,
                        agent_ceiling: editAgents,
                        token_ceiling: editTokens,
                      },
                );
                setDetail(await api<Detail>('/tasks/' + selected));
                setResourcesOpen(false);
                setNotice('Resource limits updated. Saved work retained.');
              })
            }
          >
            Apply resource limits <Check size={17} />
          </button>
        </DialogContent>
      </Dialog>
      <AlertDialog open={cancelOpen} onOpenChange={setCancelOpen}>
        <AlertDialogContent className="nexus-dialog">
          <AlertDialogTitle>Cancel this mission?</AlertDialogTitle>
          <AlertDialogDescription>
            Execution will stop. Completed work, artifacts, and the activity
            ledger remain available.
          </AlertDialogDescription>
          <div className="dialog-actions">
            <button className="secondary" onClick={() => setCancelOpen(false)}>
              Keep running
            </button>
            <button
              className="danger-button"
              disabled={!!busy}
              onClick={() =>
                void act('cancel', async () => {
                  await control('cancel');
                  setCancelOpen(false);
                })
              }
            >
              Cancel mission
            </button>
          </div>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
