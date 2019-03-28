#!/usr/bin/env bash

function _determine_fsfreeze_pod {
    local HOST_IP="$1"
    kubectl get pod -l benji-backup.me/component=fsfreeze -o json \
        | jq -r '.items ?// [.] | .[] | select(.status.hostIP=="'"$HOST_IP"'" and .status.phase == "Running") | .metadata.name'
}

function benji::backup::ceph::snapshot::create::pre {
    local VERSION_NAME="$1"
    local CEPH_POOL="$2"
    local CEPH_RBD_IMAGE="$3"
    local CEPH_RBD_SNAPSHOT="$4"

    [[ $FSFREEZE == "no" ]] && return 0

    echo "Freezing filesystem $CEPH_RBD_IMAGE_MOUNTPOINT on host $K8S_PV_HOST_IP."

    FSFREEZE_POD="$(_determine_fsfreeze_pod "$K8S_PV_HOST_IP")"
    if [[ $FSFREEZE_POD ]]; then
        if ! kubectl exec -c fsfreeze "$FSFREEZE_POD" -- fsfreeze --freeze "$CEPH_RBD_IMAGE_MOUNTPOINT"; then
            echo "Freezing filesystem failed."
            return 1
        fi
    else
        echo "Unable to determine fsfreeze pod name."
        return 1
    fi

    echo "Freezing $CEPH_RBD_IMAGE_MOUNTPOINT on host $K8S_PV_HOST_IP succeeded."
    return 0
}

function benji::backup::ceph::snapshot::create::post::error {
    local VERSION_NAME="$1"
    local CEPH_POOL="$2"
    local CEPH_RBD_IMAGE="$3"
    local CEPH_RBD_SNAPSHOT="$4"

    [[ $FSFREEZE == "no" ]] && return 0

    echo "Unfreezing filesystem $CEPH_RBD_IMAGE_MOUNTPOINT on host $K8S_PV_HOST_IP."

    # Retry three times in rapid succession and then wait a bit for the last two retries
    for try in 0 1 1 1 15 30; do
        sleep "$try"

        # We try to determine the pod name at each iteration so that we'll detect a newly started pod and use it
        FSFREEZE_POD="$(_determine_fsfreeze_pod "$K8S_PV_HOST_IP")"
        if [[ $FSFREEZE_POD ]]; then
            if kubectl exec -c fsfreeze "$FSFREEZE_POD" -- fsfreeze --unfreeze "$CEPH_RBD_IMAGE_MOUNTPOINT"; then
                echo "Unfreezing $CEPH_RBD_IMAGE_MOUNTPOINT on host $K8S_PV_HOST_IP succeeded."
                return 0
            else
                echo "Unfreezing filesystem failed, retrying."
            fi
        else
            echo "Unable to determine fsfreeze pod name, retrying."
        fi
    done

    # We reach this point when we've exhausted all tries
    echo "Giving up on unfreezing $CEPH_RBD_IMAGE_MOUNTPOINT on host $K8S_PV_HOST_IP."
    return 1
}

function benji::backup::ceph::snapshot::create::post::success {
    benji::backup::ceph::snapshot::create::post::error "$@"
}

function benji::backup::pre {
    local VERSION_NAME="$1"

    START_TIME=$(date +'%s')
    benji_backup_start_time -command=backup -auxiliary_data=initial -version_name="$VERSION_NAME" set $(date +'%s.%N')

    return 0
}

function _k8s_create_pvc_event {
    local TYPE="$1"
    local REASON="$2"
    shift 2
    local MESSAGE="$*"

    local POD_NAME="$POD_NAME"
    [[ $POD_NAME ]] || POD_NAME="$BENJI_INSTANCE"

    local K8S_PVC_UID="$(kubectl get pvc --namespace "$K8S_PVC_NAMESPACE" "$K8S_PVC_NAME" -o json | jq -r '.metadata.uid')"
    local EC=$?; [[ $EC == 0 ]] || return $EC

    # Setting uid is required so that kubectl describe finds the event.
    # And setting firstTimestamp is required so that kubectl shows a proper age for it.
    # See: https://github.com/kubernetes/kubernetes/blob/a92729a301c8928d8e108418e6e4625a9e0d6733/pkg/kubectl/describe/versioned/describe.go#L3281
    kubectl create -f - <<EOF
apiVersion: v1
kind: Event
metadata:
  name: "$BENJI_INSTANCE-$(uuidgen --time)"
  namespace: "$K8S_PVC_NAMESPACE"
  labels:
    reporting-component: benji-backup-pvc
    reporting-instance: "$POD_NAME"
    benji-backup.me/instance: "$BENJI_INSTANCE"
involvedObject:
  apiVersion: v1
  kind: PersistentVolumeClaim
  name: "$K8S_PVC_NAME"
  namespace: "$K8S_PVC_NAMESPACE"
  uid: "$K8S_PVC_UID"
eventTime: "$(date --utc '+%FT%T.%6NZ')"
firstTimestamp: "$(date --utc '+%FT%T.%6NZ')"
lastTimestamp: "$(date --utc '+%FT%T.%6NZ')"
type: "$TYPE"
reason: "$REASON"
message: "$MESSAGE"
action: None
reportingComponent: benji-backup-pvc
reportingInstance: "$POD_NAME"
source:
  component: benji-backup-pvc
EOF
}

function benji::backup::post::error {
    local VERSION_NAME="$1"
    local BENJI_BACKUP_STDERR="$2"

    benji_backup_status_failed -command=backup -auxiliary_data=initial -version_name="$VERSION_NAME" set 1
    benji_backup_completion_time -command=backup -auxiliary_data=initial -version_name="$VERSION_NAME" set $(date +'%s.%N')
    benji_backup_runtime_seconds -command=backup -auxiliary_data=initial -version_name="$VERSION_NAME" set $[$(date +'%s') - $START_TIME]

    _k8s_create_pvc_event Warning FailedBackup "Backup failed: $(grep 'ERROR:' <<<"$BENJI_BACKUP_STDERR" | tail -3 | \
        tr '\n' ',' | tr --squeeze-repeats '\t ')"

    return 0
}

function _format_version_stats {
    local VERSION_UID="$1"

    benji -m --log-level "$BENJI_LOG_LEVEL" stats "uid == '$VERSION_UID'" | \
        jq -r '.stats[0] | "(took " + (.duration | tostring) + " seconds, " + (.bytes_written | tostring) + " bytes written)"'
}

function benji::backup::post::success {
    local VERSION_NAME="$1"
    local BENJI_BACKUP_STDERR="$2"
    local VERSION_UID="$3"

    benji_backup_status_succeeded -command=backup -auxiliary_data=initial -version_name="$VERSION_NAME" set 1
    benji_backup_completion_time -command=backup -auxiliary_data=initial -version_name="$VERSION_NAME" set $(date +'%s.%N')
    benji_backup_runtime_seconds -command=backup -auxiliary_data=initial -version_name="$VERSION_NAME" set $[$(date +'%s') - $START_TIME]

    _k8s_create_pvc_event Normal SuccessfulBackup "Backup to $VERSION_UID completed successfully" \
        "$(_format_version_stats $VERSION_UID)"

    return 0
}

function benji::command::pre {
    START_TIME=$(date +'%s')
    benji_command_start_time -command="$COMMAND" -auxiliary_data= -arguments="$*" set "$(date +'%s.%N')"

    return 0
}

function benji::command::post::error {
    benji_command_status_failed -command="$COMMAND" -auxiliary_data= -arguments="$*" set 1
    benji_command_completion_time -command="$COMMAND" -auxiliary_data= -arguments="$*" set "$(date +'%s.%N')"
    benji_command_runtime_seconds -command="$COMMAND" -auxiliary_data= -arguments="$*" set $[$(date +'%s') - $START_TIME]

    return 0
}

function benji::command::post::success {
    benji_command_status_succeeded -command="$COMMAND" -auxiliary_data= -arguments="$*" set 1
    benji_command_completion_time -command="$COMMAND" -auxiliary_data= -arguments="$*" set "$(date +'%s.%N')"
    benji_command_runtime_seconds -command="$COMMAND" -auxiliary_data= -arguments="$*" set $[$(date +'%s') - $START_TIME]

    return 0
}
