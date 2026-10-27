output "invoker_bindings" {
  description = "Triage-tier run.invoker bindings managed by this root (service => member)"
  value = {
    agent_invoker_bridge = "${google_cloud_run_v2_service_iam_member.agent_invoker_bridge.name} => ${google_cloud_run_v2_service_iam_member.agent_invoker_bridge.member}"
    agent_invoker_agent  = "${google_cloud_run_v2_service_iam_member.agent_invoker_agent.name} => ${google_cloud_run_v2_service_iam_member.agent_invoker_agent.member}"
    bridge_invoker_agent = "${google_cloud_run_v2_service_iam_member.bridge_invoker_agent.name} => ${google_cloud_run_v2_service_iam_member.bridge_invoker_agent.member}"
    tools_invoker_agent  = "${google_cloud_run_v2_service_iam_member.tools_invoker_agent.name} => ${google_cloud_run_v2_service_iam_member.tools_invoker_agent.member}"
  }
}
