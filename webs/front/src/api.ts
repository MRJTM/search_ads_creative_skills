import type { AgentChatResponse, MaterialsResponse, SkillSummary } from './types';

const API_BASE: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';

export function imageUrl(filename: string): string {
  return `${API_BASE}/materials/${filename}`;
}

export async function fetchMaterials(): Promise<MaterialsResponse> {
  const res = await fetch(`${API_BASE}/api/materials`);
  if (!res.ok) throw new Error(`素材加载失败（${res.status}）`);
  return (await res.json()) as MaterialsResponse;
}

export async function fetchSkills(): Promise<SkillSummary[]> {
  const res = await fetch(`${API_BASE}/api/skills`);
  if (!res.ok) throw new Error(`Skills 加载失败（${res.status}）`);
  return (await res.json()) as SkillSummary[];
}

export async function sendChat(
  message: string,
  sessionId: string,
): Promise<AgentChatResponse> {
  const res = await fetch(`${API_BASE}/api/agent/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId }),
  });
  if (!res.ok) throw new Error(`Agent 请求失败（${res.status}）`);
  return (await res.json()) as AgentChatResponse;
}
