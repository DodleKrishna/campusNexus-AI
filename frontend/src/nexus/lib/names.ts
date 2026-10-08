/** "Aditi Rao" → "Aditi"; "Dr. Ashok Verma" → "Dr. Verma". */
export function friendlyName(displayName: string): string {
  const parts = displayName.trim().split(/\s+/);
  if (/^(dr|prof)\.?$/i.test(parts[0] ?? "") && parts.length > 1) return `${parts[0]} ${parts[parts.length - 1]}`;
  return parts[0] ?? displayName;
}
