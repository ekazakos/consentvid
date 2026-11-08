// Cloudflare Worker entry: cron only (no HTTP endpoint).
import { runOnce } from './bot.js'

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runOnce(env, { now: event.scheduledTime || Date.now() }))
  },
}
