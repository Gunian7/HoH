export const name = "dsh-plugin-hoh";
export const inject = ["webServer"];

const DEFAULT_HOH_URL = "http://127.0.0.1:8765";

async function request(path, options = {}) {
  const response = await fetch(`${process.env.HOH_URL ?? DEFAULT_HOH_URL}${path}`, {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers ?? {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error ?? `HoH request failed: ${response.status}`);
  return body;
}

export function createHohClient() {
  return {
    health: () => request("/health"),
    listTasks: () => request("/v1/tasks"),
    getTask: (taskId) => request(`/v1/tasks/${encodeURIComponent(taskId)}`),
    getEvents: (taskId) => request(`/v1/tasks/${encodeURIComponent(taskId)}/events`),
    createTask: (task) => request("/v1/tasks", {
      method: "POST",
      body: JSON.stringify(task),
    }),
    previewPlan: (planReq) => request("/v1/plans/preview", {
      method: "POST",
      body: JSON.stringify(planReq),
    }),
    materializePlan: (planReq) => request("/v1/plans/materialize", {
      method: "POST",
      body: JSON.stringify(planReq),
    }),
  };
}

export function apply(ctx) {
  const webServer = ctx.get("webServer");
  const client = createHohClient();
  webServer.register({
    method: "GET",
    path: "/api/hoh/health",
    handler: () => client.health(),
  });
  webServer.register({
    method: "GET",
    path: "/api/hoh/tasks",
    handler: () => client.listTasks(),
  });
  webServer.register({
    method: "POST",
    path: "/api/hoh/plans/preview",
    handler: (req) => client.previewPlan(req.body),
  });
  webServer.register({
    method: "POST",
    path: "/api/hoh/plans/materialize",
    handler: (req) => client.materializePlan(req.body),
  });
}
