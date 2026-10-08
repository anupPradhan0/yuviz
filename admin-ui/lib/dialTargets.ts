// Mirrors libs/config_sdk/dial_targets.py: numbers are +digits only, so typed spacing is
// stripped; sip:user@host URIs keep their dots and dashes.
export function normalizeDialTarget(value: string | null | undefined): string | null {
  const v = value?.trim();
  if (!v) return null;
  return /^sips?:/i.test(v) ? v : v.replace(/[\s\-().]/g, "");
}
