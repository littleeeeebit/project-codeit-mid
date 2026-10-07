Global gates on paid work:
- `budget_settings.paid_enabled`, set together with paid admission by the owner's `paid on/off`;
- `budget_settings.frozen_reason`, set by an overrun settlement;
- `database_control.paid_admission`, false during maintenance or after a failed validation;
- gateway ownership;
- the presence of any unknown attempt.
`reserve` refuses on the first three and on ownership. `mark_dispatching` checks paid admission, ownership and unknown attempts again at dispatch time.