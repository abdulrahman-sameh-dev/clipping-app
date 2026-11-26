const storedBase = window.localStorage.getItem("clipping.apiBase");
export const API_BASE = (storedBase || window.CLIPPING_API_BASE || "").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message, { category = "internal_error", status = 0, details = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.category = category;
    this.status = status;
    this.details = details;
  }
}

function pathWithQuery(path, params) {
  if (!params) return path;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      query.set(key, String(value));
    }
  }
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
}

async function parseResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    return null;
  }
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function errorFromPayload(payload, status) {
  const envelope = payload?.error;
  if (envelope && typeof envelope === "object") {
    return new ApiError(envelope.message || "The request failed.", {
      category: envelope.category || "internal_error",
      status,
      details: envelope,
    });
  }
  if (payload?.detail) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg || String(item)).join("; ")
      : String(payload.detail);
    return new ApiError(detail, { category: "invalid_request", status, details: payload.detail });
  }
  return new ApiError(`The request failed with status ${status}.`, {
    category: status === 404 ? "not_found" : "internal_error",
    status,
  });
}

async function request(path, { method = "GET", body, signal } = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("The local backend is not reachable. Start it with `uv run clipping-ai serve`.", {
      category: "provider_unavailable",
      status: 0,
    });
  }
  const payload = await parseResponse(response);
  if (!response.ok) {
    throw errorFromPayload(payload, response.status);
  }
  return payload;
}

function uploadVideo(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/api/video/upload`);
    xhr.responseType = "json";
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.loaded / event.total);
      }
    });
    xhr.addEventListener("load", () => {
      const payload = xhr.response;
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload);
      } else {
        reject(errorFromPayload(payload, xhr.status));
      }
    });
    xhr.addEventListener("error", () => {
      reject(
        new ApiError("The upload could not reach the local backend.", {
          category: "provider_unavailable",
          status: 0,
        }),
      );
    });
    xhr.addEventListener("abort", () => {
      reject(new ApiError("Upload cancelled.", { category: "cancelled", status: 0 }));
    });
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

function get(path, params, options) {
  return request(pathWithQuery(path, params), options);
}

function post(path, body) {
  return request(path, { method: "POST", body });
}

function patch(path, body) {
  return request(path, { method: "PATCH", body });
}

function del(path, body) {
  return request(path, { method: "DELETE", body });
}

export const api = {
  health: () => get("/health"),
  status: (includeHealth = false, options = {}) =>
    get("/api/ai/status", { include_health: includeHealth }, options),
  providers: () => get("/api/ai/providers"),
  models: (task) => get("/api/ai/models", { task }),
  usage: (days = 7, recentLimit = 50) =>
    get("/api/ai/usage", { days, recent_limit: recentLimit }),
  testConnection: (provider, model = null) =>
    post("/api/ai/test-connection", { provider, model }),
  reloadProviders: () => post("/api/ai/reload"),
  toggleProvider: (providerId, enabled) =>
    patch(`/api/ai/providers/${encodeURIComponent(providerId)}`, { enabled }),
  discoverProviderModels: (providerId, force = true) =>
    post(
      pathWithQuery(`/api/ai/providers/${encodeURIComponent(providerId)}/discover`, {
        force,
      }),
      undefined,
    ),
  skills: () => get("/api/skills"),
  toggleSkill: (skillId, enabled) =>
    patch(`/api/skills/${encodeURIComponent(skillId)}`, { enabled }),
  reviewSkill: (skillId, reviewed = true) =>
    post(`/api/skills/${encodeURIComponent(skillId)}/review`, { reviewed }),
  executeSkill: (skillId, context = {}) =>
    post(`/api/skills/${encodeURIComponent(skillId)}/execute`, { context }),

  videoConfig: () => get("/api/video/config"),
  analysisConfig: () => get("/api/analysis/config"),
  clipsConfig: () => get("/api/clips/config"),

  listVideoJobs: (params = {}, options = {}) => get("/api/video/jobs", params, options),
  getVideoJob: async (id, options = {}) =>
    (await get(`/api/video/jobs/${encodeURIComponent(id)}`, undefined, options)).job,
  sourceMediaUrl: (id) => `${API_BASE}/api/video/jobs/${encodeURIComponent(id)}/media`,
  getVideoStatus: (id) => get(`/api/video/jobs/${encodeURIComponent(id)}/status`),
  retryVideo: (id) => post(`/api/video/jobs/${encodeURIComponent(id)}/retry`),
  cancelVideo: (id) => post(`/api/video/jobs/${encodeURIComponent(id)}/cancel`),
  deleteVideo: (id) => del(`/api/video/jobs/${encodeURIComponent(id)}`),
  restoreVideo: (id) => post(`/api/video/jobs/${encodeURIComponent(id)}/restore`),
  purgeVideo: (id) => del(`/api/video/jobs/${encodeURIComponent(id)}/permanent`),
  setSourcePublished: (id, published) =>
    post(`/api/video/jobs/${encodeURIComponent(id)}/publish`, { published }),
  regenerateSourceMetadata: (id, payload = {}) =>
    post(`/api/video/jobs/${encodeURIComponent(id)}/metadata/regenerate`, payload),
  uploadVideo,
  addVideoUrl: (url) => post("/api/video/url", { url }),

  transcribeVideo: (id, payload = {}) =>
    post(`/api/video/jobs/${encodeURIComponent(id)}/transcribe`, payload),
  analyzeVideo: (id, payload = {}) =>
    post(`/api/video/jobs/${encodeURIComponent(id)}/analyze`, payload),
  highlightsVideo: (id, payload = {}) =>
    post(`/api/video/jobs/${encodeURIComponent(id)}/highlights`, payload),
  getTranscript: (id) => get(`/api/video/jobs/${encodeURIComponent(id)}/transcript`),
  getHighlights: (id) => get(`/api/video/jobs/${encodeURIComponent(id)}/highlights`),
  createClips: (id, payload = {}) =>
    post(`/api/video/jobs/${encodeURIComponent(id)}/clips`, payload),
  listSourceClips: (id, limit = 100) =>
    get(`/api/video/jobs/${encodeURIComponent(id)}/clips`, { limit }),

  listClipJobs: (params = {}, options = {}) => get("/api/clip-jobs", params, options),
  getClipJob: async (id) => (await get(`/api/clip-jobs/${encodeURIComponent(id)}`)).job,
  getClipStatus: (id) => get(`/api/clip-jobs/${encodeURIComponent(id)}/status`),
  updateClipVisual: (id, payload = {}) =>
    patch(`/api/clip-jobs/${encodeURIComponent(id)}/visual`, payload),
  renderClip: (id) => post(`/api/clip-jobs/${encodeURIComponent(id)}/render`),
  retryClip: (id) => post(`/api/clip-jobs/${encodeURIComponent(id)}/retry`),
  cancelClip: (id) => post(`/api/clip-jobs/${encodeURIComponent(id)}/cancel`),
  deleteClip: (id) => del(`/api/clip-jobs/${encodeURIComponent(id)}`),
  restoreClip: (id) => post(`/api/clip-jobs/${encodeURIComponent(id)}/restore`),
  purgeClip: (id) => del(`/api/clip-jobs/${encodeURIComponent(id)}/permanent`),
  updateClipMetadata: (id, payload) =>
    patch(`/api/clip-jobs/${encodeURIComponent(id)}/metadata`, payload),
  regenerateClipMetadata: (id, payload = {}) =>
    post(`/api/clip-jobs/${encodeURIComponent(id)}/metadata/regenerate`, payload),
  setClipPublished: (id, published) =>
    post(`/api/clip-jobs/${encodeURIComponent(id)}/publish`, { published }),
  listTrash: () => get("/api/trash"),
  restoreTrash: (items) => post("/api/trash/restore", { items }),
  purgeTrash: (items) => post("/api/trash/permanent", { items }),
  emptyTrash: () => del("/api/trash"),
  clipDownloadUrl: (id) => `${API_BASE}/api/clip-jobs/${encodeURIComponent(id)}/download`,
};
