# ROS 2 / Gazebo scope (AI-assisted; 2026-10-30)

| Claim | Evidence / version / date | Status | Limits |
|---|---|---|---|
| ROS 2 publishes source under multiple open-source licences, commonly Apache-2.0 | ROS 2 repository: https://github.com/ros2/ros2 and individual package LICENSE files; referenced 2026-10-30 | Unverified live (links supplied; licence must be checked per dependency) | Do not treat the whole ROS ecosystem as one licence |
| Gazebo components have their own open-source licence notices | https://github.com/gazebosim/gz-sim ; referenced 2026-10-30 | Unverified live | Verify exact release and transitive components before distribution |
| ROS 2 / Gazebo is a multi-package, OS-sensitive installation, unlike this standard-library mock | https://docs.ros.org/en/rolling/Installation.html and https://gazebosim.org/docs ; referenced 2026-10-30 | Unverified live | No install-size or supported-distro measurement performed |
| An offline fixture can be tested without ROS middleware or Gazebo | `adapters/ros2.py`, `examples/robots/test_ros2_mock.py` | Pending Colab test | No interoperability claim |

## Boundary

This mock is observe-only and **simulation-fixture-only**. It imports no `rclpy`, opens no sockets, subscribes to no topics, publishes no commands, and never connects to Gazebo or a physical device. It accepts caller-injected synthetic joint position/time/step; `targets()` is empty. `apply`, `dry_run`, `advance`, and `clear_stop` reject calls. `stop` is a no-op because there is no transport or actuator. Do not register it as a real ROS adapter or infer safety of actual ROS data. A future real observe-only adapter needs documented topic schema, provenance, bounded input handling, a threat model, and Part A review before any integration. No write path is in scope.
