/**
 * GET /api/health
 * Returns service status and whether the API key is configured.
 * The ANTHROPIC_API_KEY is set in Cloudflare Pages → Settings → Environment Variables.
 */
export async function onRequestGet(context) {
  const { env } = context;
  return Response.json({
    status: "ok",
    api_key_configured: Boolean(env.ANTHROPIC_API_KEY),
  });
}
