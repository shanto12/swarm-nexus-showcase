import { proxy } from '../lib/proxy';
const handler = (request: Request) => proxy(request);
export default handler;
export const config = { path: '/api/*' };
