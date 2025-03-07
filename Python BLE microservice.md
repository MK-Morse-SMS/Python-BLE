- **FastAPI** as the web framework (which natively supports asynchronous endpoints and can stream Server-Sent Events).
- **Bleak** as the BLE library for scanning, connecting, and handling notifications.
- An **asynchronous architecture** so that scanning, connection management, and notifications can run concurrently.

An api that 
1. Lists connected devices (/gap/nodes?connection_state=connected)
	```json
	{
		"nodes": [{
			"bdaddr": "EF:A3:E6:94:CD:2D",
			"name": "FRM",
			"connectionState": "connected"
		}]
	}
	```
2. Adds devices to a connection list. Any devices in the list should connect automatically and reconnect automatically if they are disconnected. 
`curl -v -X POST -H "content-type: application/json" -d '{"list":["C0:00:5B:D1:B7:25","C0:00:5B:D1:AF:F0"]}' 'http://172.16.10.99/gap/batch-connect'`
3. List Characteristics on a connected device (/gatt/nodes/<MAC>/characteristics)
4. Enable notifications on a characteristic POST (/gatt/nodes/<MAC>/characteristic/<UUID>)
5. Disable notifications on a characteristic DELETE (/gatt/nodes/<MAC>/characteristic/<UUID>)
6. Disable notifications on all characteristics
7. Disconnect from a device DELETE (/gatt/nodes/<MAC>)
8. Start scanning while filtering to only devices advertising one of the provided service uuids, service uuids should be provided when starting a scan. Scanning should be asynchronous and restart every 5 min.
9. Send Notifications, Connections, Disconnections, and live scan results using Server Sent Events