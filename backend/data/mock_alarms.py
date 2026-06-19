MOCK_ALARMS: dict[str, list[dict]] = {
    "LINK_DOWN": [
        {
            "alarm_id": "ALM-001",
            "alarm_type": "LINK_DOWN",
            "ne_name": "HNI-CORE-01",
            "content": "Interface GigabitEthernet0/0/1 on NE HNI-CORE-01 (10.211.140.16) is DOWN; peer AS65001; cell HNI_0231",
            "severity": "CRITICAL",
            "event_time": "2026-06-10 09:15:00",
        },
        {
            "alarm_id": "ALM-002",
            "alarm_type": "LINK_DOWN",
            "ne_name": "HNI-CORE-02",
            "content": "Interface GigabitEthernet0/0/2 on NE HNI-CORE-02 (10.211.140.17) is DOWN",
            "severity": "MAJOR",
            "event_time": "2026-06-10 09:18:00",
        },
    ],
    "BGP_SESSION_DOWN": [
        {
            "alarm_id": "ALM-003",
            "alarm_type": "BGP_SESSION_DOWN",
            "ne_name": "HNI-BORDER-01",
            "content": "BGP session to peer 10.0.0.1 on NE HNI-BORDER-01 (10.211.140.1) is DOWN; AS65002",
            "severity": "CRITICAL",
            "event_time": "2026-06-10 09:20:00",
        },
    ],
}
