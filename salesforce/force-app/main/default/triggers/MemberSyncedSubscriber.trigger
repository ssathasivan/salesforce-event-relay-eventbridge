trigger MemberSyncedSubscriber on Member_Synced__e (after insert) {
    // Retry cap for transient errors. Stays well below the platform limit
    // that suspends the trigger.
    final Integer MAX_RETRIES = 3;
    final String LOG_SOURCE = 'MemberSyncedSubscriber';
    Map<Id, Member__c> updates = new Map<Id, Member__c>();
    Map<Id, String> sourceEventByMember = new Map<Id, String>();
    List<Relay_Error_Log__c> logs = new List<Relay_Error_Log__c>();
    for (Member_Synced__e evt : Trigger.new) {
        // Member_Id__c is a Text field. Validate it before using it as an Id,
        // so one bad value is skipped and logged instead of failing the batch.
        Id memberId;
        try {
            memberId = Id.valueOf(evt.Member_Id__c);
            if (memberId.getSobjectType() != Member__c.SObjectType) {
                memberId = null;
            }
        } catch (Exception e) {
            memberId = null;
        }
        if (memberId == null) {
            logs.add(RelayErrorLog.entry(LOG_SOURCE, null, evt.Source_Event_Id__c,
                'Invalid Member_Id__c: ' + evt.Member_Id__c));
            continue;
        }
        updates.put(memberId, new Member__c(
            Id                    = memberId,
            External_Member_Id__c = evt.External_Member_Id__c,
            Sync_Status__c        = evt.Sync_Status__c));
        sourceEventByMember.put(memberId, evt.Source_Event_Id__c);
    }
    List<Member__c> toUpdate = updates.values();
    // allOrNone=false: one bad record must not fail the whole batch
    List<Database.SaveResult> results = Database.update(toUpdate, false);
    Boolean transientFailure = false;
    List<Relay_Error_Log__c> failureLogs = new List<Relay_Error_Log__c>();
    for (Integer i = 0; i < results.size(); i++) {
        if (results[i].isSuccess()) {
            continue;
        }
        for (Database.Error err : results[i].getErrors()) {
            if (err.getStatusCode() == StatusCode.UNABLE_TO_LOCK_ROW) {
                transientFailure = true;
            }
        }
        Id memberId = toUpdate[i].Id;
        failureLogs.add(RelayErrorLog.entry(LOG_SOURCE, memberId, sourceEventByMember.get(memberId),
            String.valueOf(results[i].getErrors())));
    }
    // The update is idempotent (same external id for the same member), so retrying
    // the whole batch is safe. Throwing rolls back this transaction, logs included.
    if (transientFailure && EventBus.TriggerContext.currentContext().retries < MAX_RETRIES) {
        throw new EventBus.RetryableException('Row lock on Member__c, retrying batch');
    }
    logs.addAll(failureLogs);
    RelayErrorLog.write(logs);
}
