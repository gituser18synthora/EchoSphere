/* Mirrors the backend password policy (backend/core/security.py) so admins get
   instant feedback while typing; the API remains the enforcement boundary. */
export const PASSWORD_POLICY_HINT =
  "At least 8 characters with an uppercase letter, a lowercase letter and a digit.";

export function passwordPolicyError(password: string): string | null {
  const missing: string[] = [];
  if (password.length < 8) missing.push("at least 8 characters");
  if (!/[a-z]/.test(password)) missing.push("a lowercase letter");
  if (!/[A-Z]/.test(password)) missing.push("an uppercase letter");
  if (!/\d/.test(password)) missing.push("a digit");
  return missing.length ? `Password must contain ${missing.join(", ")}.` : null;
}
