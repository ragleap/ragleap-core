# Where approvals are accepted

Approvals ("YES <id>" or "NO <id>") count only when they come from a private, one-to-one conversation with the owner.

- Telegram: the approval target must be a private chat with the owner (a positive number). A group, supergroup or channel id (a negative number) is not accepted when you save the setting, and an approval typed in a group is ignored.
- Discord: use a direct message conversation. An approval typed in a server channel is ignored.
- Why: in a group the id that arrives belongs to the group, so everyone in it would look like the owner. Approvals from the dashboard or the API are not affected.

This helps an operator keep approvals with one person. It is not a certification of any kind.
