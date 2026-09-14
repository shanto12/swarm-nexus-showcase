export type Assessment = {
  complexity: string;
  basis: string;
  reasons: string[];
  recommended_agents?: number;
  initial_token_allocation?: number;
  parallel_width?: number;
  dependency_depth?: number;
  assignments?: number;
};
export type Resources = {
  mode: 'auto' | 'manual';
  agent_ceiling: number;
  token_ceiling: number;
  assessment: Assessment;
};
export type Mission = {
  id: string;
  prompt: string;
  status: string;
  agent_limit: number;
  active_agents: number;
  token_budget: number;
  tokens_used: number;
  tokens_uncertain: number;
  tokens_reserved: number;
  estimated_cost_usd: number;
  created_at: string;
  updated_at: string;
  result: string | null;
  blocker: string | null;
  resources: Resources;
  repair_round: number;
};
export type Item = {
  id: string;
  title: string;
  role: string;
  status: string;
  dependencies: string[];
  assigned_agent?: string;
  attempt: number;
  output?: string;
  error?: string;
};
export type Event = {
  id: number;
  agent: string;
  kind: string;
  message: string;
  created_at: string;
  item_id?: string;
};
export type Artifact = {
  id: string;
  name: string;
  size: number;
  created_at: string;
  media_type: string;
};
export type Detail = {
  task: Mission;
  items: Item[];
  events: Event[];
  artifacts: Artifact[];
  handoffs: {
    id: number;
    source_item_id: string;
    target_item_id: string;
    message: string;
    created_at: string;
  }[];
};
export type Health = {
  status: string;
  service?: string;
  provider_configured: boolean;
  auth_configured: boolean;
  observability: { configured: boolean; project: string; endpoint: string };
};
export type User = { id: string; email: string };
export type ToolInventory = {
  tools: { name: string; description: string; enabled?: boolean }[];
  capacity: { max_agents_per_task: number; max_global_agents: number };
  limits: { execution: string };
};
