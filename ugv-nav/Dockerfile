# ugv-nav image: LiDAR, Rover URDF, laser odometry/EKF and the pi-agent bridge. No UART.
FROM ros:humble-ros-base-jammy

ARG UGV_WS_COMMIT=9d2e8947d77432b80f73ae758dca594aada08307
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update && apt-get install -y --no-install-recommends \
      git python3-pip ros-humble-robot-state-publisher ros-humble-joint-state-publisher ros-humble-xacro ros-humble-robot-localization \
    && rm -rf /var/lib/apt/lists/*

# Only the vendor packages we use, from the pinned ugv_ws commit.
WORKDIR /opt/ugv_ws
RUN git init -q upstream \
    && git -C upstream remote add origin https://github.com/waveshareteam/ugv_ws.git \
    && git -C upstream sparse-checkout init --cone \
    && git -C upstream sparse-checkout set src/ugv_else/ldlidar src/ugv_else/rf2o_laser_odometry \
         src/ugv_main/ugv_description \
    && git -C upstream fetch -q --depth 1 --filter=blob:none origin "$UGV_WS_COMMIT" \
    && git -C upstream checkout -q FETCH_HEAD \
    && mkdir src \
    && mv upstream/src/ugv_else/ldlidar upstream/src/ugv_else/rf2o_laser_odometry \
         upstream/src/ugv_main/ugv_description src/ \
    && rm -rf upstream \
    && source /opt/ros/humble/setup.bash \
    && colcon build --executor sequential --packages-select ldlidar rf2o_laser_odometry ugv_description \
         --cmake-args -DBUILD_TESTING=OFF -DCMAKE_BUILD_TYPE=Release \
    && rm -rf build log

COPY launch /app/launch
COPY config /app/config
# Our helpers ship as the wheel built by `uv build --package ai-ugv-car-nav-tools`;
# rclpy and message packages come from ROS itself.
COPY dist/ai_ugv_car_nav_tools-*.whl /tmp/
RUN pip3 install --no-cache-dir --no-deps /tmp/ai_ugv_car_nav_tools-*.whl \
    && rm /tmp/ai_ugv_car_nav_tools-*.whl

ENV HOME=/tmp \
    ROS_LOCALHOST_ONLY=1 \
    ROS_DOMAIN_ID=42 \
    ROS_LOG_DIR=/tmp/ros/log \
    UGV_MODEL=ugv_rover \
    LDLIDAR_MODEL=ld19 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.title="ugv-nav" \
      org.opencontainers.image.source="https://github.com/alfred-bot-001/ai_ugv_car" \
      vip.aimaker.ugv-ws.commit="${UGV_WS_COMMIT}"

USER 1000:1000
WORKDIR /tmp
CMD ["bash", "-c", "source /opt/ugv_ws/install/setup.bash && exec ros2 launch /app/launch/${UGV_NAV_LAUNCH:-n1_odometry}.launch.py"]
