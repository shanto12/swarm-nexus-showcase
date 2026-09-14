import capture from '../public/capture/mission.json';
import tools from '../public/capture/tools.json';
export const SHOWCASE = import.meta.env.VITE_SHOWCASE !== 'false';
export const capturedMissionId = capture.task.id;
export async function capturedResponse(path: string, body?: unknown): Promise<unknown> {
  if (body !== undefined) throw new Error('This public walkthrough is read-only. Live missions run in the authenticated workspace.');
  if (path === '/health') return {status:'ok',service:'captured-execution',provider_configured:false,auth_configured:false,observability:{configured:false,project:'',endpoint:''}};
  if (path === '/session') return {user:{id:'public-reviewer',email:''}};
  if (path === '/tasks') return {tasks:[capture.task]};
  if (path === '/tools') return tools;
  if (path === '/tasks/'+capture.task.id) return capture;
  throw new Error('This captured walkthrough does not provide that live endpoint.');
}
