// Publishes Customer_Created__e when a customer (Account) is created.
// Event Relay forwards the event to Amazon EventBridge.
trigger CustomerCreatedPublisher on Account (after insert) {
    List<Customer_Created__e> events = new List<Customer_Created__e>();
    for (Account acc : Trigger.new) {
        // Keep the payload small: the ID plus the fields AWS needs.
        events.add(new Customer_Created__e(
            Account_Id__c = acc.Id,
            Name__c       = acc.Name,
            Industry__c   = acc.Industry));
    }

    // The event uses PublishAfterCommit, so it is sent only if the save succeeds.
    // A failed publish doesn't throw an exception, so check each result.
    List<Database.SaveResult> results = EventBus.publish(events);
    for (Integer i = 0; i < results.size(); i++) {
        if (!results[i].isSuccess()) {
            System.debug(LoggingLevel.ERROR,
                'Customer_Created__e publish failed for ' + events[i].Account_Id__c
                + ': ' + results[i].getErrors());
        }
    }
}
