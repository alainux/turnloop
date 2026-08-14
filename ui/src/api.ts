export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const kind = response.headers.get("content-type") ?? "";
  const body: unknown = kind.includes("json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const record =
      body && typeof body === "object"
        ? (body as Record<string, unknown>)
        : null;
    throw new ApiError(
      String(record?.detail ?? record?.message ?? body ?? response.statusText),
      response.status,
    );
  }
  return body as T;
}

export function json(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}
